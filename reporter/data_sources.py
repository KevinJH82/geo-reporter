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

def _import_commons():
    """把 /opt/deepexplor-services 加入 sys.path 以导入 commons.* broker。"""
    import sys
    _repo = "/opt/deepexplor-services"
    if _repo not in sys.path:
        sys.path.insert(0, _repo)


def _bbox(min_lon, min_lat, max_lon, max_lat):
    return (min_lon, min_lat, max_lon, max_lat)


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
    matches = find_datacolle_for_bbox(_bbox(min_lon, min_lat, max_lon, max_lat))
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
    for entry in find_alteration_for_bbox(_bbox(min_lon, min_lat, max_lon, max_lat)):
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
    for entry in find_exploration_for_bbox(_bbox(min_lon, min_lat, max_lon, max_lat)):
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
        if category_id == "geophysics":
            from commons.datacolle_broker import find_datacolle_for_bbox
            m = find_datacolle_for_bbox(bbox)
            if m:
                figs.extend(m[0].get("figures", []))
        elif category_id == "remote_sensing":
            from commons.analyser_broker import find_alteration_for_bbox
            from commons.exploration_broker import find_exploration_for_bbox
            for e in find_alteration_for_bbox(bbox):
                figs.extend(e.get("figures", []))
            for e in find_exploration_for_bbox(bbox):
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

_GEO_INSAR_DOWNLOADS = "/opt/deepexplor-services/geo-insar/downloads"


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
        # 地质章节:直连地质数据 + geo-stru 本地构造解译 + data-colle 地质资料(本地实证,优先于 Web)
        return (fetch_geology(lat, lon)
                + fetch_structural_local(min_lon, min_lat, max_lon, max_lat)
                + fetch_datacolle_section("geology", min_lon, min_lat, max_lon, max_lat))
    elif category_id == "geophysics":
        # 地球物理章节:从 data-colle 读取本研究区物探资料
        return fetch_datacolle_section("geophysics", min_lon, min_lat, max_lon, max_lat)
    elif category_id == "geochemistry":
        # 地球化学章节:从 data-colle 读取本研究区化探资料
        return fetch_datacolle_section("geochemistry", min_lon, min_lat, max_lon, max_lat)
    elif category_id == "hydrology":
        return fetch_hydrology(min_lon, min_lat, max_lon, max_lat)
    elif category_id == "insar_deformation":
        return fetch_insar_local(lat, lon, min_lon, min_lat, max_lon, max_lat)
    elif category_id == "remote_sensing":
        # 遥感影像章节:geo-analyser 蚀变分析 + geo-exploration 矿产深部探测
        return (fetch_alteration_local(min_lon, min_lat, max_lon, max_lat)
                + fetch_deep_detection_local(min_lon, min_lat, max_lon, max_lat))
    return []


def fetch_structural_local(
    min_lon: float, min_lat: float,
    max_lon: float, max_lat: float,
) -> List[str]:
    """
    扫描 geo-stru 标准构造解译输出,把与本研究区相交的构造统计转成中文文本,
    注入"地质与矿产"章节,作为本 AOI 影像实测的硬本地实证(优先于 Web 搜索)。
    复用 commons/structural_broker(bbox 相交发现 + metadata.json 读取)。
    """
    import sys
    _repo = "/opt/deepexplor-services"
    if _repo not in sys.path:
        sys.path.insert(0, _repo)
    try:
        from commons.structural_broker import find_structural_for_bbox
    except Exception:
        return []

    texts: List[str] = []
    for entry in find_structural_for_bbox((min_lon, min_lat, max_lon, max_lat)):
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
        text += "- 数据契约: commons/structural_schema.json v1\n"
        text += "- 说明: 构造解译为遥感地形自动提取的决策支持产物,断裂位置/方向需野外核实。\n"
        texts.append(text)
    return texts
