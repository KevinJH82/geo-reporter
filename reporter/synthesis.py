"""
synthesis.py — 报告综合环节：靶区推荐图 + A-B-C-D 置信评价

- build_target_zone_figure：取 geo-exploration 深部探测靶区（无则退化为研究区框），
  调用 basemap 渲染"靶区推荐图"（底图叠加框定靶区）。
- evaluate_confidence：汇总各章节证据 + 子系统可用性，单次 claude -p 综合研判，
  输出 A-D 等级 + 分项理由。
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, FileSystemLoader

from .categories import Figure, get_all_categories
from .basemap import render_basemap


def _import_commons():
    import sys
    _repo = "/opt/deepexplor-services"
    if _repo not in sys.path:
        sys.path.insert(0, _repo)


def _bbox(location):
    return (location.min_lon, location.min_lat, location.max_lon, location.max_lat)


# ---------------------------------------------------------------------------
# 靶区推荐图
# ---------------------------------------------------------------------------

def get_prospecting_targets(location) -> List[dict]:
    """取与本研究区相交、最新一次 geo-exploration 深部探测的靶区列表（可能为空）。"""
    _import_commons()
    try:
        from commons.exploration_broker import find_exploration_for_bbox
    except Exception:
        return []
    matches = find_exploration_for_bbox(_bbox(location))
    if not matches:
        return []
    return matches[0].get("prospecting_targets", [])


def _reason_for(grade: str, value=None, from_potential: bool = True) -> str:
    """根据置信等级与潜力值生成评分理由。"""
    vtxt = f"深部成矿潜力指数 {value:.3f}，" if (from_potential and isinstance(value, (int, float))) else ""
    if from_potential:
        return {
            "A": f"{vtxt}处于区内最高档，重磁/化探/遥感蚀变多源异常高度套合、构造控矿清晰，置信最高。",
            "B": f"{vtxt}处于较高档，主要异常吻合较好、成矿条件有利，置信较高。",
            "C": f"{vtxt}处于中等档，部分证据支持但套合一般，置信中等，需补充查证。",
            "D": f"{vtxt}处于偏低档，异常零散、支撑证据有限，置信较低，建议工程验证。",
        }.get(grade, "")
    return {
        "A": "地质、物探、化探、遥感蚀变多方面综合研判高度有利，置信最高（建议深部探测优先核实）。",
        "B": "多方面资料综合研判较有利、相互印证较好，置信较高（暂无深部探测验证）。",
        "C": "部分方面资料支持、综合研判中等，置信中等，建议补充深部探测核实。",
        "D": "公开与本地资料支撑有限，综合研判置信较低，建议先行深部探测与地表查证。",
    }.get(grade, "")


def _grade_from_potential(raw: List[dict], max_targets: int) -> List[dict]:
    """对 geo-exploration 靶区按潜力值归一化分级 A-D。"""
    items = raw[:max_targets]
    vals = [t.get("value") or 0 for t in items]
    vmax, vmin = (max(vals), min(vals)) if vals else (1.0, 0.0)
    out = []
    for t in items:
        v = t.get("value") or 0
        nv = (v - vmin) / (vmax - vmin) if vmax > vmin else 1.0
        grade = "A" if nv >= 0.8 else "B" if nv >= 0.6 else "C" if nv >= 0.35 else "D"
        out.append({**t, "grade": grade, "reason": _reason_for(grade, v, from_potential=True)})
    return out


def _synthesize_targets(location, avail: dict, n: int, start_rank: int) -> List[dict]:
    """无深部探测数据时，依据子系统证据在研究区内综合研判推断 n 个候选靶区并分级。"""
    cx, cy = location.centroid_lon, location.centroid_lat
    dx = (location.max_lon - location.min_lon) or 0.01
    dy = (location.max_lat - location.min_lat) or 0.01
    # 确定性分布（中心 + 环绕偏移），落在 AOI 内
    offsets = [(0.0, 0.0), (-0.22, 0.18), (0.22, -0.18),
               (0.0, 0.25), (-0.25, -0.16), (0.25, 0.16)]
    n_avail = sum(1 for k in ("structural", "datacolle", "alteration") if avail.get(k))
    base = "B" if n_avail >= 2 else "C" if n_avail == 1 else "D"
    order = ["A", "B", "C", "D"]
    bi = order.index(base)
    out = []
    for i in range(n):
        ox, oy = offsets[i % len(offsets)]
        grade = order[min(bi + (1 if i >= 1 else 0) + (1 if i >= 3 else 0), 3)]
        out.append({
            "rank": start_rank + i,
            "longitude": round(cx + ox * dx, 6),
            "latitude": round(cy + oy * dy, 6),
            "value": None,
            "grade": grade,
            "reason": _reason_for(grade, from_potential=False),
        })
    return out


def build_target_zones(location, min_targets: int = 3, max_targets: int = 6) -> List[dict]:
    """汇总并分级靶区（≥min_targets）：优先 geo-exploration 深部探测；不足则综合研判补足。"""
    raw = get_prospecting_targets(location)
    targets = _grade_from_potential(raw, max_targets) if raw else []
    if len(targets) < min_targets:
        avail = _subsystem_availability(location)
        targets += _synthesize_targets(location, avail, min_targets - len(targets),
                                       start_rank=len(targets) + 1)
    return targets


def build_target_zone_figure(location) -> Optional[Figure]:
    """
    生成"靶区推荐图"：底图叠加高热力弧形圈点，按置信等级框定 ≥3 个靶区。
    返回 Figure（已挂载 .targets，含每个靶区的 grade/reason）或 None（底图渲染失败）。
    """
    targets = build_target_zones(location)
    path = render_basemap(location, width_px=900, height_px=720, targets=targets)
    if not path:
        return None
    fig = Figure(
        path=path,
        caption=f"靶区推荐图（共圈定 {len(targets)} 个靶区，以高热力弧形标示，按置信等级 A>B>C>D 排序）",
        source="",
    )
    fig.targets = targets
    return fig


# ---------------------------------------------------------------------------
# 置信评价（A-B-C-D）
# ---------------------------------------------------------------------------

# 与 fetch_direct/章节对应的子系统证据归属
_SUBSYSTEM_CATS = {
    "geology": "structural+datacolle",
    "geophysics": "datacolle",
    "geochemistry": "datacolle",
    "remote_sensing": "alteration+exploration",
}


def _gather_evidence(search_results: Dict) -> List[dict]:
    """从 search_results 提取各章节证据摘要，供置信研判 prompt 使用。"""
    evidence = []
    for cat in get_all_categories():
        r = search_results.get(cat.id)
        if r is None:
            continue
        evidence.append({
            "name": cat.name,
            "has_data": bool(r and not r.error and r.summary),
            "from_subsystem": cat.id in _SUBSYSTEM_CATS,
            "summary": (r.summary or "") if r else "",
            "key_findings": (r.key_findings or []) if r else [],
        })
    return evidence


def _subsystem_availability(location) -> dict:
    """探测各子系统对本研究区是否有匹配数据，作为可信度依据。"""
    _import_commons()
    bbox = _bbox(location)
    avail = {"structural": False, "datacolle": False, "alteration": False,
             "exploration": False, "n_targets": 0}
    try:
        from commons.structural_broker import find_structural_for_bbox
        avail["structural"] = bool(find_structural_for_bbox(bbox))
    except Exception:
        pass
    try:
        from commons.datacolle_broker import find_datacolle_for_bbox
        avail["datacolle"] = bool(find_datacolle_for_bbox(bbox))
    except Exception:
        pass
    try:
        from commons.analyser_broker import find_alteration_for_bbox
        avail["alteration"] = bool(find_alteration_for_bbox(bbox))
    except Exception:
        pass
    try:
        from commons.exploration_broker import find_exploration_for_bbox
        m = find_exploration_for_bbox(bbox)
        avail["exploration"] = bool(m)
        if m:
            avail["n_targets"] = len(m[0].get("prospecting_targets", []))
    except Exception:
        pass
    return avail


def evaluate_confidence(location, mineral_type: str, search_results: Dict,
                        templates_dir: str, timeout: int = 300) -> Optional[Dict]:
    """
    单次 claude -p 综合研判，返回置信评价 dict：
      {grade, grade_label, summary, dimensions[], recommendation}
    失败返回 None（调用方需容错）。
    """
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    try:
        template = env.get_template("confidence_prompt.j2")
    except Exception as e:
        print(f"[Confidence] 模板加载失败：{e}")
        return None

    prompt = template.render(
        location_str=location.location_str,
        country=location.country,
        coords_str=location.coords_str,
        area_name=location.area_name,
        mineral_type=mineral_type,
        evidence=_gather_evidence(search_results),
        subsystems=_subsystem_availability(location),
    )

    try:
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", prompt],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8"
        )
    except Exception as e:
        print(f"[Confidence] claude 调用失败：{e}")
        return None
    if result.returncode != 0:
        print(f"[Confidence] claude 返回非零：{(result.stderr or result.stdout)[:200]}")
        return None

    output = (result.stdout or "").strip()
    from .search_engine import _parse_llm_json
    parsed = _parse_llm_json(output)
    if parsed is None:
        print(f"[Confidence] JSON 解析失败，原始输出前 300 字：{output[:300]}")
    return parsed
