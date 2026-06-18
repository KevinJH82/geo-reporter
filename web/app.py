"""
Flask Web Backend for Geo-Reporter
处理 KML 上传、并行搜索、报告生成和下载。
"""

import os
import sys
import json
import uuid
import threading
import traceback
from pathlib import Path
from datetime import datetime

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import yaml
from flask import Flask, request, jsonify, send_file, Response

from reporter.kml_parser import parse_kml, KMLParseError
from reporter.tabular_parser import parse_tabular, TabularParseError
from reporter.geocoder import create_location_context, GeocoderError
from reporter.search_engine import SearchEngine, SearchEngineError
from reporter.report_builder import ReportBuilder


# 配置
BASE_DIR = Path(__file__).parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
REPORTS_DIR = BASE_DIR / "reports"
TEMPLATES_DIR = BASE_DIR / "templates"

for _d in (UPLOADS_DIR, REPORTS_DIR, BASE_DIR / "cache"):
    _d.mkdir(parents=True, exist_ok=True)

# 加载 config.yaml
_cfg_path = BASE_DIR / "config.yaml"
with open(_cfg_path, "r", encoding="utf-8") as _f:
    APP_CONFIG = yaml.safe_load(_f)

_ext_cfg = APP_CONFIG.get("extraction", {})
TAVILY_API_KEY = APP_CONFIG.get("tavily_api_key", "") or os.environ.get("TAVILY_API_KEY", "")
ANTHROPIC_API_KEY = APP_CONFIG.get("anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")

ALLOWED_EXTENSIONS = {".kml", ".kmz", ".ovkml", ".ovkmz", ".csv", ".xlsx", ".xls"}




def _parse_uploaded_file(file_path: str):
    """根据文件后缀选择解析器，统一返回 (geometry, bbox, name, area_name, description)"""
    suffix = Path(file_path).suffix.lower()
    if suffix in (".kml", ".kmz", ".ovkml", ".ovkmz"):
        return parse_kml(file_path)
    elif suffix in (".csv", ".xlsx", ".xls"):
        return parse_tabular(file_path)
    else:
        raise ValueError(f"不支持的文件格式: {suffix}")


# Flask 应用
app = Flask(__name__, template_folder=str(BASE_DIR / "web" / "templates"))
# ── 内部鉴权:拒绝绕过 BFF 的直连(PORTAL_INTERNAL_KEY 配置后生效) ──
try:
    import sys as _ia_sys
    if '/opt/deepexplor-services' not in _ia_sys.path:
        _ia_sys.path.insert(0, '/opt/deepexplor-services')
    from commons.internal_auth import init_internal_auth as _init_internal_auth
    _init_internal_auth(app)
except Exception as _ia_e:
    print(f'[internal_auth] 跳过接入: {_ia_e}')
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB 限制

# 任务状态跟踪
tasks = {}


@app.route("/", methods=["GET"])
def index():
    """主页"""
    return open(BASE_DIR / "web" / "templates" / "index.html").read(), 200, {
        "Content-Type": "text/html; charset=utf-8",
        # 禁止浏览器缓存前端，避免改版后仍显示旧页面（如旧的 8 类进度）
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


@app.route("/api/upload-kml", methods=["POST"])
def upload_kml():
    """
    上传地理文件（KML/KMZ/ovKML/CSV/Excel）并验证。

    Returns
    -------
    {
        "task_id": "...",
        "status": "kml_uploaded",
        "kml_name": "...",
        "area_name": "..."
    }
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"不支持的文件格式 {suffix}，仅支持 .kml / .ovkml / .kmz / .csv / .xlsx / .xls"}), 400

    # 生成任务 ID
    task_id = str(uuid.uuid4())[:8]

    # 保存并解析文件
    upload_path = UPLOADS_DIR / f"{task_id}_{file.filename}"
    try:
        file.save(str(upload_path))
        geometry, bbox, name, area_name, description = _parse_uploaded_file(str(upload_path))
        tasks[task_id] = {
            "status": "kml_uploaded",
            "kml_path": str(upload_path),
            "kml_name": file.filename,
            "geometry": geometry,
            "bbox": bbox,
            "name": name,
            "area_name": area_name,
            "description": description,
            "created_at": datetime.now().isoformat()
        }
        return jsonify({
            "task_id": task_id,
            "status": "kml_uploaded",
            "kml_name": file.filename,
            "area_name": area_name
        }), 200

    except (KMLParseError, TabularParseError) as e:
        return jsonify({"error": f"文件解析错误: {str(e)}"}), 400

    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@app.route("/api/run/<task_id>", methods=["GET"])
def run_report_generation(task_id: str):
    """
    开始生成报告，流式推送进度。

    Returns
    -------
    Server-Sent Events stream
    """
    if task_id not in tasks:
        return jsonify({"error": "Task not found"}), 404

    mineral_type = request.args.get("mineral", "").strip()
    tenant_id = request.headers.get("X-Tenant-Id")   # P2 隔离:BFF 经反代/适配器注入

    def generate_events():
        def ev(payload):
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def keepalive():
            return ": keepalive\n\n"

        def pump_keepalive(fn, box, interval=8):
            """在后台线程运行 fn()，期间每 interval 秒 yield 一次 SSE 心跳，避免长任务静默导致连接超时。
            结果写入 box['result']，异常写入 box['error']。"""
            done = threading.Event()

            def _worker():
                try:
                    box["result"] = fn()
                except Exception as exc:  # noqa: BLE001
                    box["error"] = exc
                finally:
                    done.set()

            threading.Thread(target=_worker, daemon=True).start()
            while not done.wait(timeout=interval):
                yield ": keepalive\n\n"

        try:
            task = tasks[task_id]

            # 步骤 1：地理定位
            yield ev({'step': 1, 'message': '正在确定地理位置...'})
            try:
                location = create_location_context(
                    task["bbox"],
                    task["area_name"],
                    task["description"]
                )
            except GeocoderError as e:
                yield ev({'error': f'Geocoding failed: {str(e)}'})
                return

            # 步骤 2-9：串行搜索 8 类数据
            yield ev({'step': 2, 'message': '正在搜索数据...（Tavily 并发搜索 + Claude API 提取，预计 30-60 秒）'})
            try:
                search_engine = SearchEngine(
                    templates_dir=str(TEMPLATES_DIR),
                    tavily_api_key=TAVILY_API_KEY,
                    tavily_max_results=_ext_cfg.get("tavily_max_results", 5),
                    tavily_search_depth=_ext_cfg.get("tavily_search_depth", "advanced"),
                    cache_db=str(BASE_DIR / "cache" / "geo_cache.db")
                )
                search_results = {}

                for item in search_engine.search_all_categories_stream(location, mineral_type=mineral_type):
                    if item[0] == "keepalive":
                        yield ": keepalive\n\n"
                        continue
                    idx, total, cat_id, result = item
                    search_results[cat_id] = result
                    yield ev({'step': 'search_progress', 'idx': idx, 'total': total,
                               'cat_id': cat_id, 'cat_name': result.category_name,
                               'success': not bool(result.error), 'error_msg': result.error or ''})

                total = len(search_results)
                success = sum(1 for r in search_results.values() if not r.error)
                yield ev({'step': 3, 'message': f'数据搜索完成：{success}/{total} 个类别成功'})

                if success == 0:
                    yield ev({'warning': '所有搜索都失败了。这可能是由于 API 限流。请稍后重试。'})

            except SearchEngineError as e:
                yield ev({'error': f'搜索失败：{str(e)}。请稍后重试。'})
                return

            # 步骤 3.5：靶区推荐图 + 综合置信评价（后台线程 + 心跳，避免长任务静默断连）
            yield ev({'step': 'synthesis', 'message': '正在统一研判靶区与综合置信...（同一次 Claude 研判，二者自洽，预计 30-90 秒）'})
            from reporter.synthesis import evaluate_synthesis

            def _do_synthesis():
                try:
                    # 统一研判：靶区评级与综合置信共用同一证据上下文、单次产出，保证逻辑闭合
                    return evaluate_synthesis(location, mineral_type, search_results, str(TEMPLATES_DIR))
                except Exception as exc:
                    print(f"[Synthesis] 统一研判失败：{exc}")
                    return None, None

            syn_box = {}
            yield from pump_keepalive(_do_synthesis, syn_box)
            target_figure, confidence = syn_box.get("result", (None, None))
            if confidence:
                yield ev({'step': 'confidence', 'message': f"综合置信评价：{confidence.get('grade','?')} 级（{confidence.get('grade_label','')}）"})

            # 步骤 10：生成报告（后台线程 + 心跳，期间会联网拼接底图，耗时较长）
            yield ev({'step': 4, 'message': '正在生成报告...'})
            build_box = {}

            def _do_build():
                report_builder = ReportBuilder(str(REPORTS_DIR))
                return report_builder.build_report(
                    location, search_results, output_name=task["area_name"],
                    mineral_type=mineral_type, target_figure=target_figure, confidence=confidence,
                    tenant_id=tenant_id)

            yield from pump_keepalive(_do_build, build_box)
            if "error" in build_box:
                yield ev({'error': f'Report generation failed: {build_box["error"]}'})
                return
            report_path, pptx_path = build_box["result"]
            task["report_path"] = report_path
            task["pptx_path"] = pptx_path
            task["status"] = "completed"
            yield ev({'step': 5, 'message': '报告生成完成！', 'report_path': report_path, 'pptx_path': pptx_path})

        except Exception as e:
            traceback.print_exc()
            yield ev({'error': f'Unexpected error: {str(e)}'})

    resp = Response(generate_events(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/download/<task_id>", methods=["GET"])
def download_report(task_id: str):
    """
    下载生成的报告文件。
    支持 ?format=pptx 下载 PPT 格式。

    Returns
    -------
    Word (.docx) 或 PowerPoint (.pptx) 文件
    """
    if task_id not in tasks:
        return jsonify({"error": "Report not found or not yet generated"}), 404

    fmt = request.args.get("format", "docx")

    if fmt == "pptx":
        pptx_path = tasks[task_id].get("pptx_path")
        if not pptx_path or not Path(pptx_path).exists():
            return jsonify({"error": "PPTX file not found"}), 404
        return send_file(
            pptx_path,
            mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            as_attachment=True,
            download_name=Path(pptx_path).name
        )

    report_path = tasks[task_id].get("report_path")
    if not report_path or not Path(report_path).exists():
        return jsonify({"error": "Report file not found on disk"}), 404

    return send_file(
        report_path,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=Path(report_path).name
    )


@app.route("/api/status/<task_id>", methods=["GET"])
def get_task_status(task_id: str):
    """获取任务状态"""
    if task_id not in tasks:
        return jsonify({"error": "Task not found"}), 404

    task = tasks[task_id]
    return jsonify({
        "task_id": task_id,
        "status": task.get("status", "unknown"),
        "created_at": task.get("created_at"),
        "has_report": "report_path" in task
    }), 200


@app.route("/api/cleanup/<task_id>", methods=["DELETE"])
def cleanup_task(task_id: str):
    """清理任务（删除上传和生成的文件）"""
    if task_id not in tasks:
        return jsonify({"error": "Task not found"}), 404

    task = tasks[task_id]

    # 删除上传文件
    if "kml_path" in task and Path(task["kml_path"]).exists():
        try:
            Path(task["kml_path"]).unlink()
        except Exception as e:
            app.logger.warning(f"Failed to delete KML file: {e}")

    # 删除报告文件
    if "report_path" in task and Path(task["report_path"]).exists():
        try:
            Path(task["report_path"]).unlink()
        except Exception as e:
            app.logger.warning(f"Failed to delete report file: {e}")

    # 从任务列表中删除
    del tasks[task_id]

    return jsonify({"message": "Task cleaned up"}), 200


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8081, threaded=True)
