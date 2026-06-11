"""
Direct Data Sources — 直连全球权威 API
为特定类别提供零成本、高可信度的结构化原始数据，降级到 Tavily 搜索。

支持的类别和数据源：
  climate       → Open-Meteo（历史气候，无需 API Key）
  geography     → OpenTopoData SRTM30（高程，无需 API Key）
  geology       → Macrostrat（地层数据）+ USGS Earthquake（地震）
  hydrology     → HydroSHEDS / OpenStreetMap Overpass（河流）
"""

import json
import urllib.request
import urllib.parse
from typing import List, Optional


def _get(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "geo-reporter/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Climate — Open-Meteo 历史气候 API（免费，无需 Key）
# ---------------------------------------------------------------------------

def fetch_climate(lat: float, lon: float) -> List[str]:
    """
    从 Open-Meteo 获取研究区年均气温、降雨量、蒸发量等气候数据。
    返回可直接传入提取 prompt 的文本列表。
    """
    try:
        params = urllib.parse.urlencode({
            "latitude": lat,
            "longitude": lon,
            "start_date": "2014-01-01",
            "end_date": "2023-12-31",
            "daily": ",".join([
                "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "et0_fao_evapotranspiration",
                "windspeed_10m_max"
            ]),
            "timezone": "auto",
            "models": "ERA5"
        })
        url = f"https://archive.open-meteo.com/v1/archive?{params}"
        data = _get(url)

        daily = data.get("daily", {})
        temps_max = [t for t in daily.get("temperature_2m_max", []) if t is not None]
        temps_min = [t for t in daily.get("temperature_2m_min", []) if t is not None]
        precip    = [p for p in daily.get("precipitation_sum", []) if p is not None]
        et0       = [e for e in daily.get("et0_fao_evapotranspiration", []) if e is not None]
        wind      = [w for w in daily.get("windspeed_10m_max", []) if w is not None]

        if not temps_max:
            return []

        ann_temp_max = round(sum(temps_max) / len(temps_max), 1)
        ann_temp_min = round(sum(temps_min) / len(temps_min), 1)
        ann_precip   = round(sum(precip) / 10, 1)   # 10年总量 → 年均
        ann_et0      = round(sum(et0) / 10, 1)
        ann_wind     = round(sum(wind) / len(wind), 1)
        temp_max_val = round(max(temps_max), 1)
        temp_min_val = round(min(temps_min), 1)

        text = (
            f"[Open-Meteo ERA5 历史气候数据，2014-2023年，坐标({lat},{lon})]\n"
            f"年均最高气温：{ann_temp_max} °C\n"
            f"年均最低气温：{ann_temp_min} °C\n"
            f"历史极端最高气温：{temp_max_val} °C\n"
            f"历史极端最低气温：{temp_min_val} °C\n"
            f"年均降雨量：{ann_precip} mm\n"
            f"年均蒸散量（FAO ET0）：{ann_et0} mm\n"
            f"年均最大风速：{ann_wind} km/h\n"
            f"数据来源：Open-Meteo.com / ERA5 再分析数据集（ECMWF）"
        )
        return [text]
    except Exception as e:
        print(f"[DataSource] Open-Meteo 失败：{e}")
        return []


# ---------------------------------------------------------------------------
# Geography — OpenTopoData SRTM30 高程
# ---------------------------------------------------------------------------

def fetch_geography(lat: float, lon: float, min_lon: float, min_lat: float,
                    max_lon: float, max_lat: float) -> List[str]:
    """
    从 OpenTopoData 获取研究区多点高程，计算海拔范围。
    """
    try:
        # 9点采样（3×3网格）
        lats = [min_lat, (min_lat+max_lat)/2, max_lat]
        lons = [min_lon, (min_lon+max_lon)/2, max_lon]
        locations = "|".join(f"{la},{lo}" for la in lats for lo in lons)
        url = f"https://api.opentopodata.org/v1/srtm30m?locations={locations}"
        data = _get(url)

        elevations = [r["elevation"] for r in data.get("results", []) if r.get("elevation") is not None]
        if not elevations:
            return []

        elev_min = round(min(elevations))
        elev_max = round(max(elevations))
        elev_mean = round(sum(elevations) / len(elevations))
        area_km2 = round(
            abs(max_lon - min_lon) * 111 * abs(max_lat - min_lat) * 111 * 0.9, 1
        )

        text = (
            f"[OpenTopoData SRTM30m 高程数据，坐标范围({min_lat}-{max_lat}N, {min_lon}-{max_lon}E)]\n"
            f"海拔最低点：{elev_min} m\n"
            f"海拔最高点：{elev_max} m\n"
            f"区域平均海拔：{elev_mean} m\n"
            f"高差：{elev_max - elev_min} m\n"
            f"估算区块面积：{area_km2} km²\n"
            f"数据来源：OpenTopoData.org / NASA SRTM 30m DEM"
        )
        return [text]
    except Exception as e:
        print(f"[DataSource] OpenTopoData 失败：{e}")
        return []


# ---------------------------------------------------------------------------
# Geology — Macrostrat 地层 + USGS 地震
# ---------------------------------------------------------------------------

def fetch_geology(lat: float, lon: float) -> List[str]:
    """
    从 Macrostrat 获取地层单元，从 USGS 获取地震记录。
    """
    texts = []

    # Macrostrat 地层
    try:
        url = f"https://macrostrat.org/api/v2/units?lat={lat}&lng={lon}&response=long&format=json"
        data = _get(url)
        units = data.get("success", {}).get("data", [])[:5]
        if units:
            lines = [f"[Macrostrat 地层数据，坐标({lat},{lon})]"]
            for u in units:
                age = f"{u.get('b_age', '?')}–{u.get('t_age', '?')} Ma"
                lines.append(
                    f"地层单元：{u.get('unit_name','未知')}，时代：{age}，"
                    f"岩性：{u.get('lith','未知')}，厚度：{u.get('thick','?')} m"
                )
            lines.append("数据来源：Macrostrat.org 全球地层数据库")
            texts.append("\n".join(lines))
    except Exception as e:
        print(f"[DataSource] Macrostrat 失败：{e}")

    # USGS 地震（近50年 M≥3.0，半径200km）
    try:
        params = urllib.parse.urlencode({
            "format": "geojson",
            "latitude": lat, "longitude": lon,
            "maxradiuskm": 200,
            "minmagnitude": 3.0,
            "starttime": "1975-01-01",
            "endtime":   "2025-01-01",
            "orderby": "magnitude",
            "limit": 10
        })
        url = f"https://earthquake.usgs.gov/fdsnws/event/1/query?{params}"
        data = _get(url)
        features = data.get("features", [])
        if features:
            max_mag = features[0]["properties"]["mag"]
            count = len(features)
            lines = [
                f"[USGS 地震数据，坐标({lat},{lon})，半径200km，1975–2025年]",
                f"M≥3.0 地震记录：{count} 次（查询上限10条）",
                f"最大震级：M{max_mag}",
            ]
            for f in features[:3]:
                p = f["properties"]
                lines.append(f"  - M{p['mag']}，{p.get('place','未知')}，时间：{p.get('time','')}")
            lines.append("数据来源：USGS Earthquake Hazards Program")
            texts.append("\n".join(lines))
    except Exception as e:
        print(f"[DataSource] USGS 地震 失败：{e}")

    return texts


# ---------------------------------------------------------------------------
# Hydrology — OSM Overpass 河流查询
# ---------------------------------------------------------------------------

def fetch_hydrology(min_lon: float, min_lat: float,
                    max_lon: float, max_lat: float) -> List[str]:
    """
    通过 OSM Overpass API 查询研究区内的河流/水体要素。
    """
    try:
        bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
        query = f"""
[out:json][timeout:25];
(
  way["waterway"~"river|stream|canal"]({bbox});
  relation["waterway"="river"]({bbox});
  way["natural"="water"]({bbox});
);
out body;
"""
        encoded = urllib.parse.urlencode({"data": query})
        url = f"https://overpass-api.de/api/interpreter?{encoded}"
        data = _get(url, timeout=30)

        elements = data.get("elements", [])
        rivers = [e for e in elements if e.get("tags", {}).get("waterway") in ("river", "stream", "canal")]
        waters = [e for e in elements if e.get("tags", {}).get("natural") == "water"]

        if not elements:
            return []

        names = list({e["tags"].get("name", e["tags"].get("name:zh", "")) for e in rivers if e.get("tags")})
        names = [n for n in names if n]

        text = (
            f"[OpenStreetMap Overpass 水系数据，范围({min_lat}-{max_lat}N, {min_lon}-{max_lon}E)]\n"
            f"区内河流/水道要素数量：{len(rivers)} 条\n"
            f"水体面要素数量：{len(waters)} 个\n"
            f"已命名河流：{', '.join(names[:10]) if names else '未检测到命名河流'}\n"
            f"数据来源：OpenStreetMap / Overpass API"
        )
        return [text]
    except Exception as e:
        print(f"[DataSource] OSM Overpass 失败：{e}")
        return []


# ---------------------------------------------------------------------------
# 子系统 broker 接入（commons/*_broker）——把兄弟子系统标准输出注入对应章节
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    """仓库根目录（geo-reporter 的上一级），基于本文件位置推导，兼容不同部署前缀。"""
    from pathlib import Path
    return str(Path(__file__).resolve().parents[2])


def _import_commons():
    """把仓库根加入 sys.path 以导入 commons.* broker（兼容 /opt/Project 与 /opt 两种部署路径）。"""
    import sys
    for _repo in (_repo_root(), "/opt/deepexplor-services"):
        if _repo not in sys.path:
            sys.path.insert(0, _repo)


def _bbox(min_lon, min_lat, max_lon, max_lat):
    return (min_lon, min_lat, max_lon, max_lat)


# 各兄弟子系统标准输出目录（基于真实仓库根推导）。
# broker 内部默认写死 /opt/deepexplor-services/...，在本机不存在；所有 broker 调用必须显式传入下列路径。
_ROOT = _repo_root()
GEO_STRU_OUTPUTS        = _ROOT + "/geo-stru/results"
DATACOLLE_OUTPUTS       = _ROOT + "/data-colle/prospector/output"
GEO_ANALYSER_OUTPUTS    = _ROOT + "/geo-analyser/results"
GEO_EXPLORATION_OUTPUTS = _ROOT + "/geo-exploration/Python_Project/web_app/uploads"
GEO_INSAR_DOWNLOADS     = _ROOT + "/geo-insar/downloads"


# geo-stru 高清地质构造解译图：(metadata.products 键, 图注)
_GEO_STRU_MAPS = [
    ("map_hillshade_png", "山体阴影遥感地质构造解译图"),
    ("map_terrain_png", "地形渲染遥感地质构造解译图"),
    ("rose_diagram", "构造线方向玫瑰图"),
]


def _geo_stru_figures(bbox) -> List[dict]:
    """取 geo-stru 对本研究区的高清地质构造解译图（本地实证，优先于公开地质图）；无则返回空。"""
    _import_commons()
    try:
        from commons.structural_broker import find_structural_for_bbox, get_product_path
    except Exception as e:
        print(f"[Figures] geo-stru import 失败：{e}")
        return []
    try:
        matches = find_structural_for_bbox(bbox, GEO_STRU_OUTPUTS)
    except Exception as e:
        print(f"[Figures] geo-stru 查询失败：{e}")
        return []
    if not matches:
        return []
    entry = matches[0]
    aoi = entry.get("aoi_name", "")
    figs: List[dict] = []
    for key, caption in _GEO_STRU_MAPS:
        p = get_product_path(entry, key)
        if p:
            figs.append({"path": p,
                         "caption": f"{caption}（{aoi}，geo-stru 构造解译）",
                         "source": "geo-stru"})
    return figs


GEO_MODEL3D_OUTPUTS = _ROOT + "/geo-model3d/results"

# geo-model3d 三维立体成矿预测图：(metadata.products 键, 图注)
_GEO_MODEL3D_FIGS = [
    ("depth_profile_png", "三维成矿有利度/不确定性—深度剖面"),
]


def _geo_model3d_figures(bbox) -> List[dict]:
    """取 geo-model3d 对本研究区的三维立体成矿预测图件（深度切片/剖面）；无则返回空。"""
    _import_commons()
    try:
        from commons.model3d_broker import find_model3d_for_bbox, get_product_path
    except Exception as e:
        print(f"[Figures] geo-model3d import 失败：{e}")
        return []
    try:
        matches = find_model3d_for_bbox(bbox, GEO_MODEL3D_OUTPUTS)
    except Exception as e:
        print(f"[Figures] geo-model3d 查询失败：{e}")
        return []
    if not matches:
        return []
    entry = matches[0]
    aoi = entry.get("aoi_name", "")
    ms = entry.get("model_stats", {})
    fam = ms.get("family", "")
    band = ms.get("depth_km_band", "")
    figs: List[dict] = []
    for key, caption in _GEO_MODEL3D_FIGS:
        p = get_product_path(entry, key)
        if p:
            figs.append({"path": p,
                         "caption": f"{caption}（{aoi}，成因族 {fam}，成矿深度带 {band}km，geo-model3d 立体预测）",
                         "source": "geo-model3d"})
    # 深度切片 PNG（slice_pngs 是相对路径列表）
    for rel in (entry.get("products", {}) or {}).get("slice_pngs", []) or []:
        import os as _os
        p = _os.path.join(entry["model3d_dir"], rel)
        if _os.path.exists(p):
            figs.append({"path": p, "caption": f"三维成矿有利度深度切片（{aoi}，geo-model3d）",
                         "source": "geo-model3d"})
    return figs


_PRED_METHOD_CN = {"knowledge": "知识加权融合（无标签）", "woe": "证据权法 WofE（数据驱动）",
                   "rf": "随机森林（数据驱动）", "pu": "PU-learning 正样本-未标注半监督（数据驱动）",
                   "domain_adapt": "领域自适应跨区迁移"}


def geo_model3d_modeling_summary(bbox) -> List[str]:
    """成矿预测建模小节（方向四）：从 geo-model3d metadata 提炼预测方法/标签/特征重要性/验证。

    返回 markdown 文本块列表（可直接拼入报告）；无成果或无建模信息→空。
    """
    _import_commons()
    try:
        from commons.model3d_broker import find_model3d_for_bbox
        matches = find_model3d_for_bbox(bbox, GEO_MODEL3D_OUTPUTS)
    except Exception as e:
        print(f"[Modeling] geo-model3d 读取失败：{e}")
        return []
    if not matches:
        return []
    ms = matches[0].get("model_stats", {}) or {}
    aoi = matches[0].get("aoi_name", "")
    method = ms.get("prediction_method", "knowledge")
    ls = ms.get("label_status", {}) or {}
    fi = ms.get("feature_importance") or {}
    val = ms.get("validation", {}) or {}
    tr = ms.get("transfer") or {}

    lines = [f"### 成矿预测建模方法（AOI: {aoi}，geo-model3d）",
             f"- **预测方法**：{_PRED_METHOD_CN.get(method, method)}",
             f"- **已知矿点标签**：正样本 {ls.get('n_positive', 0)} 个"
             f"（落入网格 {ls.get('n_in_grid', 0)}，真负样本/钻孔 {ls.get('n_barren', 0)}），"
             f"{'达到' if ls.get('sufficient') else '未达'}建模阈值({ls.get('label_min', '?')})"]

    # 特征重要性（RF/PU）或证据权（WofE）
    if fi.get("method") in ("random_forest", "pu_bagging") and fi.get("importances"):
        imp = "；".join(f"{k} {v}" for k, v in list(fi["importances"].items())[:6])
        extra = (f"，OOB={fi.get('oob_score')}" if fi.get("oob_score") is not None else
                 f"，平均不确定性={fi.get('mean_uncertainty')}" if fi.get("mean_uncertainty") is not None else "")
        lines.append(f"- **可解释控矿因素（特征重要性）**：{imp}{extra}")
    elif fi.get("method") == "woe" and fi.get("weights"):
        ws = "；".join(f"{k} 对比度{v.get('contrast')}" for k, v in list(fi["weights"].items())[:6])
        lines.append(f"- **证据权对比度（WofE）**：{ws}")

    # 验证
    loo = val.get("loo_hit_rate", {}) or {}
    if loo.get("status") == "ok":
        lines.append(f"- **已知矿点捕获率验证**：前10%面积捕获 {loo.get('capture_top10pct')}、"
                     f"前20% {loo.get('capture_top20pct')}，提升度 lift₁₀={loo.get('lift_top10')}")
    kc = val.get("knowledge_consistency", {}) or {}
    if kc.get("tectonic_setting"):
        lines.append(f"- **知识校验（构造背景自洽性）**：{kc.get('note', '')}")

    # 跨区迁移
    if tr:
        lines.append(f"- **跨区迁移**：源区 {tr.get('source_aoi')}（族 {tr.get('source_family')}），"
                     f"迁移置信度 {tr.get('transfer_confidence')}，特征漂移 {tr.get('feature_shift')}")

    warns = ms.get("warnings", []) or []
    if warns:
        lines.append("- **诚实性提示**：" + "；".join(warns[:3]))

    return ["\n".join(lines)]


GEO_GEOPHYS_OUTPUTS = _ROOT + "/geo-geophys/results"

# geo-geophys 位场处理图件：(metadata.products 键, 图注前缀)
_GEO_GEOPHYS_MAP_KEYS = ["map_magnetic_rtp", "map_analytic_signal", "map_tilt"]


def _geo_geophys_figures(bbox) -> List[dict]:
    """取 geo-geophys 对本研究区的位场处理图件（化极/解析信号/倾斜角 + 磁源深度）；无则返回空。"""
    _import_commons()
    try:
        from commons.geophys_broker import find_geophys_for_bbox, get_product_path
    except Exception as e:
        print(f"[Figures] geo-geophys import 失败：{e}")
        return []
    try:
        matches = find_geophys_for_bbox(bbox, GEO_GEOPHYS_OUTPUTS)
    except Exception as e:
        print(f"[Figures] geo-geophys 查询失败：{e}")
        return []
    if not matches:
        return []
    entry = matches[0]
    aoi = entry.get("aoi_name", "")
    ms = entry.get("model_stats", {})
    eu = (ms.get("euler") or {})
    depth_txt = (f"，磁源深度中位 {eu['depth_median_m']/1000:.1f}km" if eu.get("depth_median_m") else "")
    figs: List[dict] = []
    # 图件在 products.figures（相对路径列表）
    import os as _os
    for rel in (entry.get("products", {}) or {}).get("figures", []) or []:
        p = _os.path.join(entry["geophys_dir"], rel)
        if _os.path.exists(p):
            figs.append({"path": p,
                         "caption": f"区域物探解释（{aoi}{depth_txt}，geo-geophys 位场处理，区域尺度）",
                         "source": "geo-geophys"})
    return figs


GEO_GEOCHEM_OUTPUTS = _ROOT + "/geo-geochem/results"


def _geo_geochem_figures(bbox) -> List[dict]:
    """取 geo-geochem 对本研究区的化探异常图件（元素异常/组合异常/C-A 曲线）；无则返回空。"""
    _import_commons()
    try:
        from commons.geochem_broker import find_geochem_for_bbox
    except Exception as e:
        print(f"[Figures] geo-geochem import 失败：{e}")
        return []
    try:
        matches = find_geochem_for_bbox(bbox, GEO_GEOCHEM_OUTPUTS)
    except Exception as e:
        print(f"[Figures] geo-geochem 查询失败：{e}")
        return []
    if not matches:
        return []
    entry = matches[0]
    aoi = entry.get("aoi_name", "")
    mineral = (entry.get("model_stats") or {}).get("mineral_type", "")
    figs: List[dict] = []
    # 图件在 products.figures（相对 geochem_dir 的路径列表）
    import os as _os
    for rel in (entry.get("products", {}) or {}).get("figures", []) or []:
        p = _os.path.join(entry["geochem_dir"], rel)
        if _os.path.exists(p):
            figs.append({"path": p,
                         "caption": f"地球化学异常（{aoi}，目标矿种 {mineral}，geo-geochem C-A 分形分离/多元素组合）",
                         "source": "geo-geochem"})
    return figs


def fetch_geochem_summary_text(min_lon, min_lat, max_lon, max_lat) -> List[str]:
    """从 geo-geochem 标准输出读取本研究区化探异常统计与浓集中心，供 LLM 提取（本地实证，优先采信）。"""
    _import_commons()
    try:
        from commons.geochem_broker import find_geochem_for_bbox, load_anomaly_points
    except Exception:
        return []
    try:
        matches = find_geochem_for_bbox(_bbox(min_lon, min_lat, max_lon, max_lat), GEO_GEOCHEM_OUTPUTS)
    except Exception:
        return []
    if not matches:
        return []
    entry = matches[0]
    ms = entry.get("model_stats") or {}
    lines = [f"【本地地球化学异常 - geo-geochem 子系统标准输出，AOI: {entry.get('aoi_name')}，"
             f"目标矿种: {ms.get('mineral_type', '?')}】(优先于 Web 搜索，可直接引用)"]
    ast = ms.get("anomaly_stats") or {}
    if ast:
        els = ast.get("elements_processed") or []
        comb = ast.get("combination") or {}
        lines.append(f"- 基于 {ast.get('n_points', '?')} 个化探采样点，提取 {len(els)} 种元素异常"
                     f"（{'、'.join(els)}），组合方法 {comb.get('method', '-')}。")
        thr = ast.get("thresholds") or {}
        for el, v in list(thr.items())[:8]:
            lines.append(f"  · {el} 异常下限 {v.get('threshold')}（{v.get('method', '-')}）")
    elif ms.get("prior_only"):
        po = ms["prior_only"]
        lines.append(f"- 本区无实测化探点位，仅有区域背景阈值先验（{po.get('n_threshold_elements', 0)} 种元素），未提取异常。")
    pts = load_anomaly_points(entry)
    if pts:
        lines.append(f"- 识别浓集中心 {len(pts)} 个（按强度排序，前若干）：")
        for a in pts[:8]:
            lon, lat = a.get("lon"), a.get("lat")
            loc = f"({lon:.4f}, {lat:.4f})" if isinstance(lon, (int, float)) and isinstance(lat, (int, float)) else "(-,-)"
            lines.append(f"  · #{a.get('rank')}: 中心 {loc}，面积 {a.get('area_km2', '-')} km²，衬度 {a.get('contrast', '-')}")
    for w in (ms.get("warnings") or []):
        lines.append(f"- 注意：{w}")
    return ["\n".join(lines)]


def fetch_datacolle_section(section_id: str, min_lon, min_lat, max_lon, max_lat) -> List[str]:
    """
    从 data-colle 标准输出读取与本研究区相交、最新一次成果的指定章节文本
    （section_id ∈ geology / geophysics / geochemistry）。
    """
    _import_commons()
    try:
        from commons.datacolle_broker import find_datacolle_for_bbox
    except Exception:
        return []
    matches = find_datacolle_for_bbox(_bbox(min_lon, min_lat, max_lon, max_lat), DATACOLLE_OUTPUTS)
    if not matches:
        return []
    entry = matches[0]
    text = (entry.get("sections") or {}).get(section_id, "")
    if not text:
        return []
    header = (f"【本地资料 - data-colle 子系统标准输出，AOI: {entry.get('aoi_name')}，"
              f"目标矿种: {entry.get('mineral','?')}】(优先于 Web 搜索，可直接引用)\n")
    return [header + text]


def fetch_alteration_local(min_lon, min_lat, max_lon, max_lat) -> List[str]:
    """从 geo-analyser 读取与本研究区相交的蚀变分析结果，转中文叙述文本。"""
    _import_commons()
    try:
        from commons.analyser_broker import find_alteration_for_bbox
    except Exception:
        return []
    texts: List[str] = []
    for entry in find_alteration_for_bbox(_bbox(min_lon, min_lat, max_lon, max_lat), GEO_ANALYSER_OUTPUTS):
        lines = [f"【本地蚀变分析 - AOI: {entry.get('aoi_name')}，成矿类型: {entry.get('deposit_type','')}】"
                 f"(来源: geo-analyser 标准输出，优先于 Web)"]
        for r in entry.get("results", []):
            lines.append(
                f"- 蚀变矿物「{r.get('mineral','')}」({r.get('anomaly_type','')})："
                f"{r.get('sensor','')}/{r.get('method','')} 法，异常占比 {r.get('anomaly_ratio','?')}%，"
                f"阈值 {r.get('threshold','?')}"
            )
        st = entry.get("structural", {})
        if st and st.get("association_by_mineral"):
            for m, a in st["association_by_mineral"].items():
                lines.append(
                    f"- 构造-蚀变关联「{m}」：异常像元 {a.get('n_anomaly','?')}，"
                    f"中位距断裂 {a.get('median_dist_m','?'):.0f} m，"
                    f"{a.get('buffer_m','?'):.0f} m 缓冲内占比 {a.get('frac_within_buffer',0)*100:.1f}%"
                    if isinstance(a.get('median_dist_m'), (int, float)) else
                    f"- 构造-蚀变关联「{m}」：异常像元 {a.get('n_anomaly','?')}"
                )
        lines.append("- 说明：蚀变异常为多源遥感影像自动提取的找矿决策支持产物，需野外查证。")
        texts.append("\n".join(lines))
    return texts


def fetch_deep_detection_local(min_lon, min_lat, max_lon, max_lat) -> List[str]:
    """从 geo-exploration 读取与本研究区相交的矿产深部探测结果，转中文叙述文本。"""
    _import_commons()
    try:
        from commons.exploration_broker import find_exploration_for_bbox
    except Exception:
        return []
    texts: List[str] = []
    for entry in find_exploration_for_bbox(_bbox(min_lon, min_lat, max_lon, max_lat), GEO_EXPLORATION_OUTPUTS):
        st = entry.get("statistics", {})
        targets = entry.get("prospecting_targets", [])
        lines = [
            f"【本地深部探测 - AOI: {entry.get('aoi_name')}，目标矿种: {entry.get('mineral_type','')}】"
            f"(来源: geo-exploration 标准输出，优先于 Web)",
            f"- 深部成矿潜力指数：最大 {st.get('max_value','?')}，均值 {st.get('mean_value','?')}，"
            f"标准差 {st.get('std_value','?')}",
            f"- 阈值以上异常像元数：{st.get('area_threshold','?')}",
            f"- 识别靶区数（top）：{len(targets)} 个",
        ]
        for t in targets[:5]:
            lines.append(f"  · 靶区#{t.get('rank')}：经度 {t.get('longitude'):.4f}，"
                         f"纬度 {t.get('latitude'):.4f}，潜力值 {t.get('value'):.3f}")
        lines.append("- 说明：深部探测基于舒曼波共振遥感反演，靶区为决策支持，需工程验证。")
        texts.append("\n".join(lines))
    return texts


def collect_subsystem_figures(category_id: str, min_lon, min_lat, max_lon, max_lat) -> List[dict]:
    """
    确定性收集某章节对应的子系统图件（不经 LLM，避免幻觉）。
    返回 [{path, caption, source}, ...]。
    """
    _import_commons()
    figs: List[dict] = []
    bbox = _bbox(min_lon, min_lat, max_lon, max_lat)
    try:
        if category_id == "geology":
            # 优先 geo-stru 本地高清地质构造解译图（针对 ROI、本地实证）
            figs.extend(_geo_stru_figures(bbox))
            # 无本地解译图时，回退公开 Macrostrat 地质图（覆盖区，如北美）
            if not figs:
                from .geology_map import render_geology_map
                geo_fig = render_geology_map(min_lon, min_lat, max_lon, max_lat)
                if geo_fig:
                    figs.append(geo_fig)
        elif category_id == "geophysics":
            from commons.datacolle_broker import find_datacolle_for_bbox
            m = find_datacolle_for_bbox(bbox, DATACOLLE_OUTPUTS)
            if m:
                figs.extend(m[0].get("figures", []))
        elif category_id == "geochemistry":
            # geo-geochem 真实异常图件（元素/组合异常 + C-A 曲线）
            figs.extend(_geo_geochem_figures(bbox))
        elif category_id == "remote_sensing":
            from commons.analyser_broker import find_alteration_for_bbox
            from commons.exploration_broker import find_exploration_for_bbox
            for e in find_alteration_for_bbox(bbox, GEO_ANALYSER_OUTPUTS):
                figs.extend(e.get("figures", []))
            for e in find_exploration_for_bbox(bbox, GEO_EXPLORATION_OUTPUTS):
                figs.extend(e.get("figures", []))
    except Exception as e:
        print(f"[Figures] {category_id} 图件收集失败：{e}")
    return figs


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

SUPPORTED = {"climate", "geography", "geology", "geophysics", "geochemistry",
             "hydrology", "insar_deformation", "remote_sensing"}


# ---------------------------------------------------------------------------
# InSAR 形变监测 — 从 geo-insar 标准输出读本地干涉对统计
# ---------------------------------------------------------------------------

_GEO_INSAR_DOWNLOADS = GEO_INSAR_DOWNLOADS


def fetch_insar_local(
    lat: float, lon: float,
    min_lon: float, min_lat: float,
    max_lon: float, max_lat: float,
) -> List[str]:
    """
    扫描 geo-insar 标准输出目录,把本研究区范围内的 InSAR 堆栈统计转成文本注入到
    Tavily/Claude 的 raw_data 中。优先级高于 Tavily(本地权威数据)。

    匹配规则:对每个 AOI 目录,读 metadata.json 的 aoi_bbox 判断与本研究区是否重叠。

    Returns
    -------
    List[str]: 每个匹配 AOI 一段叙述文本(中文,带具体数值和单位)
    """
    import os
    from pathlib import Path

    root = Path(_GEO_INSAR_DOWNLOADS)
    if not root.exists():
        return []

    texts: List[str] = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir():
            continue

        # 优先用 stack_index.json(汇总)
        idx_path = aoi_dir / "stack_index.json"
        summary = None
        if idx_path.exists():
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    summary = json.load(f)
            except Exception:
                summary = None

        # fallback:遍历 sentinel1_insar/<pair>/metadata.json
        pair_metas: List[dict] = []
        if summary is None:
            for sensor_dir in aoi_dir.iterdir():
                if not sensor_dir.is_dir():
                    continue
                for pair_dir in sensor_dir.iterdir():
                    meta_p = pair_dir / "metadata.json"
                    if not meta_p.exists():
                        continue
                    try:
                        with open(meta_p, "r", encoding="utf-8") as f:
                            pair_metas.append(json.load(f))
                    except Exception:
                        pass
        else:
            pair_metas = summary.get("pairs", [])

        if not pair_metas:
            continue

        # 计算覆盖区域并判断是否与本研究区相交
        bboxes = [m.get("aoi_bbox") for m in pair_metas if m.get("aoi_bbox")]
        if bboxes:
            avg_bbox = bboxes[0]  # 同一 AOI 下所有对的 bbox 一致
            ab_min_lon, ab_min_lat, ab_max_lon, ab_max_lat = avg_bbox
            # 不相交则跳过
            if (ab_max_lon < min_lon or ab_min_lon > max_lon or
                ab_max_lat < min_lat or ab_min_lat > max_lat):
                continue

        # 生成统计文本
        n = len(pair_metas)
        dates = sorted(set([m.get("master_date", "") for m in pair_metas] +
                           [m.get("slave_date", "") for m in pair_metas]))
        dates = [d for d in dates if d]
        baselines = [m.get("temporal_baseline_days") for m in pair_metas if m.get("temporal_baseline_days") is not None]
        coh_means = [m.get("stats", {}).get("coherence_mean") for m in pair_metas]
        coh_means = [v for v in coh_means if v is not None]
        disp_mins = [m.get("stats", {}).get("los_displacement_min_mm") for m in pair_metas]
        disp_mins = [v for v in disp_mins if v is not None]
        disp_maxs = [m.get("stats", {}).get("los_displacement_max_mm") for m in pair_metas]
        disp_maxs = [v for v in disp_maxs if v is not None]
        sources = sorted(set(m.get("source", "") for m in pair_metas))
        polarizations = sorted(set(m.get("polarization", "") for m in pair_metas))
        orbits = sorted(set(m.get("orbit_direction", "") for m in pair_metas))

        text = (
            f"【本地 InSAR 堆栈 - AOI: {aoi_dir.name}】(来源: geo-insar 标准输出)\n"
            f"- 干涉对数量: {n} 对\n"
            f"- 时间跨度: {dates[0] if dates else '?'} 至 {dates[-1] if dates else '?'}\n"
        )
        if baselines:
            text += (
                f"- 平均时间基线: {sum(baselines)/len(baselines):.1f} 天 "
                f"(范围 {min(baselines)}-{max(baselines)} 天)\n"
            )
        if coh_means:
            text += f"- 整体相干性均值: {sum(coh_means)/len(coh_means):.3f} (中位数 {sorted(coh_means)[len(coh_means)//2]:.3f})\n"
        if disp_mins and disp_maxs:
            text += f"- LOS 形变范围: {min(disp_mins):.2f} mm 至 {max(disp_maxs):.2f} mm\n"
        text += f"- 数据来源: {', '.join(sources)}\n"
        text += f"- 极化: {', '.join(polarizations)}; 轨道方向: {', '.join(orbits)}\n"
        text += f"- 数据契约: commons/insar_schema.json v1\n"
        texts.append(text)

    return texts


def fetch_direct(category_id: str, lat: float, lon: float,
                 min_lon: float, min_lat: float,
                 max_lon: float, max_lat: float) -> List[str]:
    """
    对支持的类别调用对应直连 API，返回原始文本列表。
    不支持的类别返回空列表（降级到 Tavily）。
    """
    if category_id == "climate":
        return fetch_climate(lat, lon)
    elif category_id == "geography":
        # 地理与地形地貌:直连 SRTM 高程 + data-colle 地形资料 + geo-stru 地形构造解译(含高程范围/线性体)
        return (fetch_geography(lat, lon, min_lon, min_lat, max_lon, max_lat)
                + fetch_datacolle_section("geography", min_lon, min_lat, max_lon, max_lat)
                + fetch_structural_local(min_lon, min_lat, max_lon, max_lat))
    elif category_id == "geology":
        # 地质章节:直连地质数据 + geo-stru 本地构造解译 + data-colle 地质资料 + geo-model3d 成矿建模小节(方向四)
        return (fetch_geology(lat, lon)
                + fetch_structural_local(min_lon, min_lat, max_lon, max_lat)
                + fetch_datacolle_section("geology", min_lon, min_lat, max_lon, max_lat)
                + geo_model3d_modeling_summary(_bbox(min_lon, min_lat, max_lon, max_lat)))
    elif category_id == "geophysics":
        # 地球物理章节:从 data-colle 读取本研究区物探资料
        return fetch_datacolle_section("geophysics", min_lon, min_lat, max_lon, max_lat)
    elif category_id == "geochemistry":
        # 地球化学章节:data-colle 资料文本 + geo-geochem 本地异常实证（追加，不替换）
        return (fetch_datacolle_section("geochemistry", min_lon, min_lat, max_lon, max_lat)
                + fetch_geochem_summary_text(min_lon, min_lat, max_lon, max_lat))
    elif category_id == "hydrology":
        return fetch_hydrology(min_lon, min_lat, max_lon, max_lat)
    elif category_id == "insar_deformation":
        return fetch_insar_local(lat, lon, min_lon, min_lat, max_lon, max_lat)
    elif category_id == "remote_sensing":
        # 遥感影像章节:geo-analyser 蚀变分析 + geo-exploration 矿产深部探测
        return (fetch_alteration_local(min_lon, min_lat, max_lon, max_lat)
                + fetch_deep_detection_local(min_lon, min_lat, max_lon, max_lat))
    return []


def _strike_group(deg: float) -> str:
    """把走向角(0–180°,正北为0顺时针)归入四组方位名。"""
    d = deg % 180.0
    if d < 22.5 or d >= 157.5:
        return "近SN(近南北)"
    if d < 67.5:
        return "NE(北东)"
    if d < 112.5:
        return "近EW(近东西)"
    return "NW(北西)"


def _structural_metallogenic_interp(st: dict) -> str:
    """
    基于 geo-stru 已落盘的构造统计,给出**与矿种无关**的通用构造控矿初步研判。
    只依据真实数值(走向分组/断裂密度/线性体规模)做"源-运-储"框架下的导/容矿
    通道判读,不引入任何具体矿种假设、不臆造未提取的构造。结论属决策支持,需野外核实。
    """
    lines: List[str] = []
    strikes = st.get("dominant_strikes_deg") or []
    groups: List[str] = []
    for d in strikes:
        try:
            g = _strike_group(float(d))
        except (TypeError, ValueError):
            continue
        if g not in groups:
            groups.append(g)

    if groups:
        if len(groups) >= 2:
            lines.append(
                f"- 构造格架:发育 {len(groups)} 组优势方向({'、'.join(groups)}),"
                f"以首组「{groups[0]}」为区域主干断裂格架,余组为横切/次级构造。"
                f"不同方向断裂的**交汇结点**(尤其主干×横切近共轭部位)是应力-流体易聚集、"
                f"成矿最有利的构造部位,应作为找矿优先靶区。"
            )
        else:
            lines.append(
                f"- 构造格架:优势方向集中于「{groups[0]}」单组,构成区域主干断裂格架;"
                f"沿该组主断裂的拐折、分支与膨大部位为有利容矿构造部位。"
            )

    dens = st.get("lineament_density_mean")
    if isinstance(dens, (int, float)):
        lines.append(
            f"- 断裂网络与导/容矿:平均断裂密度 {dens:.4f}。在「源-运-储」成矿模型中,"
            f"断裂网络承担「运」的角色——既是深部成矿流体的**导矿通道**,其交汇/密集带与"
            f"次级裂隙又构成**容矿空间**;断裂密度热点带因而是构造控矿先验高权重区。"
        )

    n = st.get("n_lineaments")
    tot = st.get("total_lineament_length_km")
    if isinstance(n, int) and n > 0 and isinstance(tot, (int, float)):
        lines.append(
            f"- 控矿研判要点:本区共提取 {n} 条线性体、总长 {tot:.1f} km;"
            f"建议将「近断裂带 + 蚀变/化探异常套合」「断裂交汇结点」「密度热点」三类位置"
            f"叠合圈定靶区(可进一步叠加 geo-analyser 蚀变、geo-exploration 深部探测收敛)。"
        )

    if not lines:
        return ""
    return "构造控矿初步研判(通用框架,与具体矿种无关):\n" + "\n".join(lines) + "\n"


def fetch_structural_local(
    min_lon: float, min_lat: float,
    max_lon: float, max_lat: float,
) -> List[str]:
    """
    扫描 geo-stru 标准构造解译输出,把与本研究区相交的构造统计转成中文文本,
    注入"地质与矿产"章节,作为本 AOI 影像实测的硬本地实证(优先于 Web 搜索)。
    复用 commons/structural_broker(bbox 相交发现 + metadata.json 读取)。

    除几何/统计事实外,还附带一段**通用构造控矿初步研判**(走向分组、共轭交汇靶区、
    密度热点=导/容矿通道、源-运-储框架),给下游 LLM 合成「成矿-构造关系」提供本地实证抓手,
    避免因缺实证而在「不得凭空捏造」约束下略过该论述。
    """
    _import_commons()
    try:
        from commons.structural_broker import find_structural_for_bbox
    except Exception:
        return []

    texts: List[str] = []
    for entry in find_structural_for_bbox((min_lon, min_lat, max_lon, max_lat), GEO_STRU_OUTPUTS):
        st = entry.get("structural_stats", {})
        n = st.get("n_lineaments")
        text = f"【本地构造解译 - AOI: {entry.get('aoi_name')}】(来源: geo-stru 标准输出)\n"
        if n is not None:
            text += f"- 自动提取线性体(断裂/线性构造): {n} 条\n"
        if st.get("total_lineament_length_km") is not None:
            text += f"- 线性体总长度: {st['total_lineament_length_km']:.1f} km\n"
        if st.get("dominant_strikes_deg"):
            strikes = "、".join(f"{d:.0f}°" for d in st["dominant_strikes_deg"])
            text += f"- 主构造方向(走向): {strikes}\n"
        if st.get("lineament_density_mean") is not None:
            text += f"- 平均断裂密度: {st['lineament_density_mean']:.4f}\n"
        if st.get("elevation_range_m"):
            er = st["elevation_range_m"]
            text += f"- 高程范围: {er[0]:.0f}–{er[1]:.0f} m\n"
        interp = _structural_metallogenic_interp(st)
        if interp:
            text += interp
        text += "- 数据契约: commons/structural_schema.json v1\n"
        text += "- 说明: 构造解译为遥感地形自动提取的决策支持产物,断裂位置/方向及控矿研判需野外核实。\n"
        texts.append(text)
    return texts
