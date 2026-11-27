"""
CLI INTERFACE PARA AGENTES DE IA (Claude Code, Open Code, Scripts)
Permite executar ações rápidas no sistema diretamente via terminal ou chamadas subprocess.
Exemplos:
  python -m app.ai_bus.cli status
  python -m app.ai_bus.cli list-videos
  python -m app.ai_bus.cli unused-windows --video-id=VIDEO_ID
  python -m app.ai_bus.cli propose-clip --agent=claude-code --video-id=VIDEO_ID --start=120.0 --end=175.0 --title="Momento épico"
  python -m app.ai_bus.cli send-event --agent=open-code --type="PING" --payload='{"msg":"hello"}'
"""

import sys
import argparse
import json
from app.database.session import SessionLocal, init_db
from app.database.models import SourceChannel, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost
from app.ai_bus.bus import AIBus

def main():
    init_db()
    parser = argparse.ArgumentParser(description="AI Bus CLI para Claude Code e Open Code")
    subparsers = parser.add_subparsers(dest="command", help="Comandos disponíveis")

    # Command: status
    subparsers.add_parser("status", help="Retorna status geral do sistema em JSON")

    # Command: list-videos
    p_videos = subparsers.add_parser("list-videos", help="Lista vídeos monitorados e seus status")
    p_videos.add_argument("--status", default=None, help="Filtrar por status (ex: transcribed, discovered)")

    # Command: unused-windows
    p_windows = subparsers.add_parser("unused-windows", help="Lista intervalos livres (não usados) de um vídeo")
    p_windows.add_argument("--video-id", required=True, help="ID do vídeo no YouTube")

    # Command: propose-clip
    p_clip = subparsers.add_parser("propose-clip", help="Registra proposta de corte com validação de duplicidade")
    p_clip.add_argument("--agent", default="claude-code", help="Nome da IA (ex: claude-code, open-code)")
    p_clip.add_argument("--video-id", required=True, help="ID do vídeo")
    p_clip.add_argument("--start", type=float, required=True, help="Início em segundos")
    p_clip.add_argument("--end", type=float, required=True, help="Fim em segundos")
    p_clip.add_argument("--title", required=True, help="Título chamativo do Short")
    p_clip.add_argument("--hook", default="", help="Frase de gancho inicial (0-3s)")
    p_clip.add_argument("--summary", default="", help="Resumo do porquê o corte é viral")
    p_clip.add_argument("--score", type=int, default=85, help="Pontuação de viralidade (0-100)")
    p_clip.add_argument("--category", default="gaming", help="Categoria")
    p_clip.add_argument("--crop", default="split_screen", choices=["split_screen", "face_tracking", "blur_bg"], help="Modo 9:16")

    # Command: list-clips
    p_list_clips = subparsers.add_parser("list-clips", help="Lista clipes registrados no banco")
    p_list_clips.add_argument("--video-id", default=None, help="Filtrar por ID do vídeo")

    # Command: read-inbox
    p_inbox = subparsers.add_parser("read-inbox", help="Lê eventos destinados ao agente")
    p_inbox.add_argument("--agent", required=True, help="Nome do agente (ex: claude-code)")

    # Command: send-event
    p_send = subparsers.add_parser("send-event", help="Envia evento para o barramento")
    p_send.add_argument("--agent", required=True, help="Nome do remetente")
    p_send.add_argument("--target", default="all", help="Destinatário")
    p_send.add_argument("--type", required=True, help="Tipo do evento")
    p_send.add_argument("--payload", default="{}", help="JSON com conteúdo")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    with AIBus() as bus:
        if args.command == "status":
            db = bus._db
            channels_count = db.query(SourceChannel).filter(SourceChannel.is_active == True).count()
            videos_count = db.query(SourceVideo).count()
            clips_count = db.query(ClipSegment).count()
            scheduled_count = db.query(ScheduledPost).filter(ScheduledPost.status == "pending").count()
            dest_count = db.query(DestinationChannel).count()

            print(json.dumps({
                "status": "online",
                "monitored_channels_active": channels_count,
                "total_videos": videos_count,
                "total_clips_registered": clips_count,
                "pending_scheduled_posts": scheduled_count,
                "destination_channels": dest_count,
                "deduplication_engine": "ACTIVE"
            }, indent=2, ensure_ascii=False))

        elif args.command == "list-videos":
            db = bus._db
            q = db.query(SourceVideo)
            if args.status:
                q = q.filter(SourceVideo.status == args.status)
            videos = q.order_by(SourceVideo.created_at.desc()).limit(30).all()
            out = [
                {
                    "id": v.id,
                    "channel_id": v.channel_id,
                    "title": v.title,
                    "duration": v.duration_seconds,
                    "status": v.status,
                    "clips_count": len(v.clips)
                }
                for v in videos
            ]
            print(json.dumps(out, indent=2, ensure_ascii=False))

        elif args.command == "unused-windows":
            res = bus.get_available_windows_for_agent(args.video_id)
            print(json.dumps(res, indent=2, ensure_ascii=False))

        elif args.command == "propose-clip":
            res = bus.propose_clip(
                agent_name=args.agent,
                video_id=args.video_id,
                start_time=args.start,
                end_time=args.end,
                title=args.title,
                hook_text=args.hook,
                summary=args.summary,
                virality_score=args.score,
                category=args.category,
                crop_mode=args.crop
            )
            print(json.dumps(res, indent=2, ensure_ascii=False))

        elif args.command == "list-clips":
            db = bus._db
            q = db.query(ClipSegment)
            if args.video_id:
                q = q.filter(ClipSegment.video_id == args.video_id)
            clips = q.order_by(ClipSegment.id.desc()).limit(50).all()
            out = [
                {
                    "id": c.id,
                    "video_id": c.video_id,
                    "start": c.start_time,
                    "end": c.end_time,
                    "duration": c.duration,
                    "title": c.title,
                    "score": c.virality_score,
                    "crop_mode": c.crop_mode,
                    "suggested_by": c.suggested_by,
                    "status": c.status
                }
                for c in clips
            ]
            print(json.dumps(out, indent=2, ensure_ascii=False))

        elif args.command == "read-inbox":
            events = bus.get_inbox(args.agent)
            print(json.dumps(events, indent=2, ensure_ascii=False))

        elif args.command == "send-event":
            try:
                p = json.loads(args.payload)
            except Exception:
                p = {"raw": args.payload}
            res = bus.publish_event(
                sender_agent=args.agent,
                event_type=args.type,
                payload=p,
                target_agent=args.target
            )
            print(json.dumps(res, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
