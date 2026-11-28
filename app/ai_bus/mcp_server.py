"""
MCP (MODEL CONTEXT PROTOCOL) SERVER FOR CLAUDE CODE & OPEN CODE
Permite que Claude Code e Open Code invoquem ferramentas do sistema nativamente via MCP (JSON-RPC stdio).
"""

import sys
import json
import traceback
from typing import Dict, Any
from app.database.session import SessionLocal, init_db
from app.database.models import SourceChannel, SourceVideo, ClipSegment, ScheduledPost, DestinationChannel
from app.services.deduplicator import DeduplicationEngine

TOOLS_DEFINITION = [
    {
        "name": "get_system_status",
        "description": "Retorna o status geral do sistema de clipping (canais monitorados, vídeos, clipes, slots agendados).",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "list_videos",
        "description": "Lista vídeos monitorados de canais (Alanzoka, Paulinho Loko, Lipao, etc.) prontos para extração de clipes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filtro de status (ex: transcribed, discovered)"}
            }
        }
    },
    {
        "name": "get_unused_windows",
        "description": "Retorna os intervalos de tempo (em segundos) que NUNCA foram cortados nem reutilizados em um determinado vídeo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string", "description": "ID do vídeo no YouTube"}
            },
            "required": ["video_id"]
        }
    },
    {
        "name": "propose_clip",
        "description": "Registra uma proposta de clipe viral validando automaticamente contra duplicidade no banco de dados.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Nome da IA (ex: claude-code, open-code)"},
                "video_id": {"type": "string", "description": "ID do vídeo"},
                "start_time": {"type": "number", "description": "Início em segundos"},
                "end_time": {"type": "number", "description": "Fim em segundos"},
                "title": {"type": "string", "description": "Título chamativo do Short"},
                "hook_text": {"type": "string", "description": "Frase de gancho para os primeiros 3 segundos"},
                "summary": {"type": "string", "description": "Por que esse corte é viral"},
                "virality_score": {"type": "integer", "description": "Score de viralidade estimado (1 a 100)"},
                "category": {"type": "string", "description": "Categoria (funny, rage, clutch, epic)"},
                "crop_mode": {"type": "string", "enum": ["split_screen", "face_tracking", "blur_bg"]}
            },
            "required": ["video_id", "start_time", "end_time", "title"]
        }
    },
    {
        "name": "get_best_moments",
        "description": "Detecta (ou lê do cache) os melhores momentos de um vídeo por picos de áudio, fala e mudanças visuais. Retorna início/fim, score, tipo e motivos. Use antes de propose_clip.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string", "description": "ID do vídeo"},
                "refresh": {"type": "boolean", "description": "Recalcula os momentos (padrão: usa os já gravados se existirem)"}
            },
            "required": ["video_id"]
        }
    },
    {
        "name": "get_schedule_matrix",
        "description": "Retorna o calendário de agendamento de postagens (4 vídeos por dia para os próximos 7 dias).",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]

def handle_call_tool(name: str, arguments: Dict[str, Any]) -> Any:
    with SessionLocal() as db:
        dedup = DeduplicationEngine(db)

        if name == "get_system_status":
            channels_count = db.query(SourceChannel).filter(SourceChannel.is_active == True).count()
            videos_count = db.query(SourceVideo).count()
            clips_count = db.query(ClipSegment).count()
            scheduled_count = db.query(ScheduledPost).filter(ScheduledPost.status == "pending").count()
            return {
                "status": "online",
                "monitored_channels": channels_count,
                "total_videos": videos_count,
                "total_clips": clips_count,
                "pending_scheduled": scheduled_count,
                "deduplication_engine": "ACTIVE"
            }

        elif name == "list_videos":
            status_filter = arguments.get("status")
            q = db.query(SourceVideo)
            if status_filter:
                q = q.filter(SourceVideo.status == status_filter)
            videos = q.order_by(SourceVideo.created_at.desc()).limit(20).all()
            return [
                {
                    "video_id": v.id,
                    "title": v.title,
                    "duration_seconds": v.duration_seconds,
                    "status": v.status,
                    "channel_id": v.channel_id,
                    "transcript_available": bool(v.transcript_path)
                }
                for v in videos
            ]

        elif name == "get_unused_windows":
            video_id = arguments["video_id"]
            video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
            if not video:
                return {"error": f"Vídeo {video_id} não encontrado no banco"}
            windows = dedup.get_available_windows(video_id, video.duration_seconds)
            used = dedup.get_used_segments(video_id)
            return {
                "video_id": video.id,
                "title": video.title,
                "duration": video.duration_seconds,
                "used_segments": used,
                "available_unused_windows": windows
            }

        elif name == "propose_clip":
            video_id = arguments["video_id"]
            start_time = float(arguments["start_time"])
            end_time = float(arguments["end_time"])
            title = arguments["title"]
            agent_name = arguments.get("agent_name", "claude_code")
            hook_text = arguments.get("hook_text", "")
            summary = arguments.get("summary", "")
            virality_score = int(arguments.get("virality_score", 85))
            category = arguments.get("category", "highlight")
            crop_mode = arguments.get("crop_mode", "split_screen")

            is_over, reason = dedup.is_overlapping(video_id, start_time, end_time)
            if is_over:
                return {
                    "success": False,
                    "error": f"Rejeitado pela proteção de duplicidade: {reason}"
                }

            seg = dedup.register_segment(
                video_id=video_id,
                start_time=start_time,
                end_time=end_time,
                title=title,
                hook_text=hook_text,
                summary=summary,
                virality_score=virality_score,
                category=category,
                suggested_by=agent_name
            )
            seg.crop_mode = crop_mode
            db.commit()

            return {
                "success": True,
                "clip_id": seg.id,
                "message": f"Clipe '{title}' registrado com sucesso e garantido contra reutilização futura!"
            }

        elif name == "get_best_moments":
            from app.database.models import VideoMoment
            from app.services.moment_detector import MomentDetector
            video_id = arguments["video_id"]
            det = MomentDetector(db)
            has_rows = db.query(VideoMoment).filter(VideoMoment.video_id == video_id).count() > 0
            if arguments.get("refresh") or not has_rows:
                det.detect(video_id)
            used = det.used_ranges(video_id)
            rows = (
                db.query(VideoMoment)
                .filter(VideoMoment.video_id == video_id)
                .order_by(VideoMoment.score.desc())
                .all()
            )
            return [det.serialize(m, used) for m in rows]

        elif name == "get_schedule_matrix":
            posts = (
                db.query(ScheduledPost)
                .order_by(ScheduledPost.scheduled_for.asc())
                .limit(28)
                .all()
            )
            return [
                {
                    "post_id": p.id,
                    "clip_id": p.clip_id,
                    "destination_channel_id": p.destination_channel_id,
                    "scheduled_for": p.scheduled_for.isoformat(),
                    "status": p.status
                }
                for p in posts
            ]

        else:
            raise ValueError(f"Ferramenta desconhecida: {name}")

def run_stdio_server():
    """Executa o loop JSON-RPC stdio do protocolo MCP"""
    init_db()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            method = req.get("method")
            msg_id = req.get("id")

            if method == "initialize":
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "shorts-clipper-mcp", "version": "1.0.0"}
                    }
                }
                sys.stdout.write(json.dumps(res) + "\n")
                sys.stdout.flush()

            elif method == "tools/list":
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": TOOLS_DEFINITION}
                }
                sys.stdout.write(json.dumps(res) + "\n")
                sys.stdout.flush()

            elif method == "tools/call":
                params = req.get("params", {})
                t_name = params.get("name")
                t_args = params.get("arguments", {})
                try:
                    result = handle_call_tool(t_name, t_args)
                    res = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                        }
                    }
                except Exception as ex:
                    res = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Erro: {str(ex)}"}],
                            "isError": True
                        }
                    }
                sys.stdout.write(json.dumps(res) + "\n")
                sys.stdout.flush()

            else:
                # Método não implementado
                if msg_id is not None:
                    res = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": "Method not found"}
                    }
                    sys.stdout.write(json.dumps(res) + "\n")
                    sys.stdout.flush()

        except Exception as e:
            sys.stderr.write(f"MCP Server Error: {traceback.format_exc()}\n")

if __name__ == "__main__":
    run_stdio_server()
