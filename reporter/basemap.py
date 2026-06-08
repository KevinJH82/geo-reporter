"""
basemap.py — 可复用的瓦片底图渲染（抽自 pptx_builder._generate_location_map）

拼接高德卫星+中文标注瓦片，绘制研究区红框；可选叠加靶区点（编号+矩形框）。
供 PPTX、Word（靶区推荐图）共用。失败时返回 None（调用方需容错）。
"""

import io
import math
import ssl
import tempfile
import urllib.request
from typing import List, Optional


def _hot_color(t: float):
    """热力色标 hot colormap：t∈[0,1]，0=暗红、0.5=橙、1=近白。"""
    t = max(0.0, min(1.0, t))
    if t < 0.4:
        return (int(110 + (255 - 110) * (t / 0.4)), 0, 0)
    if t < 0.75:
        u = (t - 0.4) / 0.35
        return (255, int(200 * u), 0)
    u = (t - 0.75) / 0.25
    return (255, int(200 + 55 * u), int(235 * u))


# 置信等级 → (热斑半径 px, 弧圈颜色)
_GRADE_RADIUS = {"A": 50, "B": 43, "C": 36, "D": 30}
_GRADE_RING = {"A": (255, 60, 30), "B": (255, 140, 0),
               "C": (0, 210, 210), "D": (130, 200, 255)}


def _draw_targets(img, targets, geo_to_px):
    """在底图上叠加每个靶区的高热力径向晕染 + 弧形圈点（弧形=留缺口的同心弧）。"""
    from PIL import Image, ImageDraw

    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    pts = []
    for t in targets:
        lon = t.get("longitude"); lat = t.get("latitude")
        if lon is None or lat is None:
            continue
        px, py = geo_to_px(lon, lat)
        grade = t.get("grade", "C")
        R = _GRADE_RADIUS.get(grade, 36)
        pts.append((px, py, R, grade))
        # 径向热力：由外(暗红/低透明)向中心(近白/高不透明)堆叠同心实心圆
        steps = 28
        for i in range(steps, 0, -1):
            frac = i / steps          # 1=最外, →0 中心
            rr = R * frac
            col = _hot_color(1.0 - frac)
            alpha = int(16 + 165 * (1.0 - frac))
            od.ellipse([px - rr, py - rr, px + rr, py + rr], fill=col + (alpha,))
    # 把热力图按 alpha 混合到底图
    img.paste(overlay, (0, 0), overlay)
    # 弧形圈点：两层留缺口的同心弧
    d = ImageDraw.Draw(img)
    for px, py, R, grade in pts:
        ring = _GRADE_RING.get(grade, (0, 210, 210))
        for rr in (R + 6, R + 13):
            d.arc([px - rr, py - rr, px + rr, py + rr], start=20, end=160, fill=ring, width=3)
            d.arc([px - rr, py - rr, px + rr, py + rr], start=200, end=340, fill=ring, width=3)
        d.line([px - 4, py, px + 4, py], fill=(255, 255, 255), width=2)
        d.line([px, py - 4, px, py + 4], fill=(255, 255, 255), width=2)


def _zoom_for_span(span: float) -> int:
    if span < 0.03:
        return 14
    elif span < 0.1:
        return 12
    elif span < 0.5:
        return 10
    elif span < 2.0:
        return 8
    elif span < 8.0:
        return 7
    return 6


def render_basemap(
    location,
    width_px: int = 660,
    height_px: int = 600,
    targets: Optional[List[dict]] = None,
    draw_aoi_box: bool = True,
    overlay_url_template: Optional[str] = None,
    overlay_opacity: float = 1.0,
) -> Optional[str]:
    """
    渲染底图 PNG，返回临时文件路径；失败返回 None。

    Parameters
    ----------
    location : LocationContext  （需含 min_lon/min_lat/max_lon/max_lat、centroid_lat/lon）
    targets : 可选靶区列表 [{longitude, latitude, rank, value}, ...]，叠加编号点与矩形框
    draw_aoi_box : 是否绘制研究区红框
    overlay_url_template : 可选叠加图层 XYZ 瓦片模板（含 {z}{x}{y}，如 Macrostrat carto）；
        与卫星+标注采用同一 Web-Mercator 切片方案，半透明叠加到底图上
    overlay_opacity : 叠加图层不透明度（0~1），仅在 overlay_url_template 非空时生效
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    try:
        min_lon = location.min_lon
        min_lat = location.min_lat
        max_lon = location.max_lon
        max_lat = location.max_lat
        center_lat = location.centroid_lat
        center_lon = location.centroid_lon

        span = max(max_lon - min_lon, max_lat - min_lat)
        zoom = _zoom_for_span(span)
        TILE_SIZE = 256

        def lon_to_tile_x(lon):
            return (lon + 180.0) / 360.0 * (2 ** zoom)

        def lat_to_tile_y(lat):
            lat_r = math.radians(lat)
            return (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * (2 ** zoom)

        cx_f = lon_to_tile_x(center_lon)
        cy_f = lat_to_tile_y(center_lat)
        cols = math.ceil(width_px / TILE_SIZE) + 2
        rows = math.ceil(height_px / TILE_SIZE) + 2
        start_x = int(cx_f - cols / 2)
        start_y = int(cy_f - rows / 2)
        canvas = Image.new("RGB", (cols * TILE_SIZE, rows * TILE_SIZE), (200, 200, 200))

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        max_tile = 2 ** zoom
        for tx in range(cols):
            for ty in range(rows):
                tile_x = (start_x + tx) % max_tile
                tile_y = (start_y + ty) % max_tile
                if tile_y < 0 or tile_y >= max_tile:
                    continue
                url_sat = (f"https://webst01.is.autonavi.com/appmaptile"
                           f"?style=6&x={tile_x}&y={tile_y}&z={zoom}")
                url_label = (f"https://wprd01.is.autonavi.com/appmaptile"
                             f"?lang=zh_cn&size=1&scl=2&style=8"
                             f"&x={tile_x}&y={tile_y}&z={zoom}")
                try:
                    req = urllib.request.Request(url_sat, headers={"User-Agent": "geo-reporter/0.1"})
                    with urllib.request.urlopen(req, timeout=8, context=ssl_ctx) as resp:
                        tile_img = Image.open(io.BytesIO(resp.read())).convert("RGBA")
                    try:
                        req2 = urllib.request.Request(url_label, headers={"User-Agent": "geo-reporter/0.1"})
                        with urllib.request.urlopen(req2, timeout=8, context=ssl_ctx) as resp2:
                            label_img = Image.open(io.BytesIO(resp2.read())).convert("RGBA")
                        tile_img = Image.alpha_composite(tile_img, label_img)
                    except Exception:
                        pass
                    # 可选叠加图层（如 Macrostrat 地质图瓦片，可能为 512²，统一缩放到 TILE_SIZE）
                    if overlay_url_template:
                        try:
                            ov_url = overlay_url_template.format(z=zoom, x=tile_x, y=tile_y)
                            req3 = urllib.request.Request(ov_url, headers={"User-Agent": "geo-reporter/0.1"})
                            with urllib.request.urlopen(req3, timeout=8, context=ssl_ctx) as resp3:
                                ov_img = Image.open(io.BytesIO(resp3.read())).convert("RGBA")
                            if ov_img.size != (TILE_SIZE, TILE_SIZE):
                                ov_img = ov_img.resize((TILE_SIZE, TILE_SIZE))
                            if overlay_opacity < 1.0:
                                alpha = ov_img.split()[3].point(lambda p: int(p * overlay_opacity))
                                ov_img.putalpha(alpha)
                            tile_img = Image.alpha_composite(tile_img, ov_img)
                        except Exception:
                            pass
                    canvas.paste(tile_img.convert("RGB"), (tx * TILE_SIZE, ty * TILE_SIZE))
                except Exception:
                    pass

        center_canvas_x = (cx_f - start_x) * TILE_SIZE
        center_canvas_y = (cy_f - start_y) * TILE_SIZE
        crop_x0 = int(center_canvas_x - width_px / 2)
        crop_y0 = int(center_canvas_y - height_px / 2)
        img = canvas.crop((crop_x0, crop_y0, crop_x0 + width_px, crop_y0 + height_px))

        def geo_to_px(lon, lat):
            fx = (lon_to_tile_x(lon) - start_x) * TILE_SIZE - crop_x0
            fy = (lat_to_tile_y(lat) - start_y) * TILE_SIZE - crop_y0
            return fx, fy

        draw = ImageDraw.Draw(img)

        if draw_aoi_box:
            bx0, by0 = geo_to_px(min_lon, max_lat)
            bx1, by1 = geo_to_px(max_lon, min_lat)
            if abs(bx1 - bx0) < 20:
                mx = (bx0 + bx1) / 2; bx0, bx1 = mx - 10, mx + 10
            if abs(by1 - by0) < 20:
                my = (by0 + by1) / 2; by0, by1 = my - 10, my + 10
            draw.rectangle([bx0, by0, bx1, by1], outline=(220, 30, 30), width=2)

        # 叠加靶区：高热力弧形圈点 + 编号(置信等级)
        if targets:
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
            except Exception:
                font = ImageFont.load_default()
            _draw_targets(img, targets, geo_to_px)
            draw = ImageDraw.Draw(img)  # 热力混合后重建 draw
            for t in targets:
                lon = t.get("longitude"); lat = t.get("latitude")
                if lon is None or lat is None:
                    continue
                px, py = geo_to_px(lon, lat)
                rank = t.get("rank", "")
                grade = t.get("grade", "")
                label = f"#{rank}" + (f"·{grade}" if grade else "")
                # 文字描边，保证在热力背景上清晰
                for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    draw.text((px + 14 + dx, py - 22 + dy), label, fill=(0, 0, 0), font=font)
                draw.text((px + 14, py - 22), label, fill=(255, 255, 255), font=font)

        # 金色外框
        for offset in range(3):
            draw.rectangle([offset, offset, width_px - 1 - offset, height_px - 1 - offset],
                           outline=(212, 168, 67))

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img.save(tmp.name, "PNG")
        tmp.close()
        return tmp.name
    except Exception:
        return None
