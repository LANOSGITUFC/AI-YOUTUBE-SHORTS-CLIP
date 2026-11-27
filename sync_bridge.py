"""
REAL-TIME SYNC BRIDGE (ANTIGRAVITY <-> CLAUDE CODE)
Executado em segundo plano ou no ambiente local (/home/ldks/Documents/work-claude/)
para manter Antigravity e Claude Code 100% sincronizados via Git e filas de tarefas.
"""

import os
import sys
import time
import subprocess
import json
import glob
from datetime import datetime

INBOX_DIR = "./data/claude_inbox"
OUTBOX_DIR = "./data/claude_outbox"

def run_cmd(cmd, cwd="."):
    res = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()

def sync_git():
    """Puxa alterações remotas (git pull) com segurança"""
    code, out, err = run_cmd("git pull origin main --rebase")
    if code != 0:
        # Se falhar rebase simples
        run_cmd("git pull origin main")

def process_pending_missions():
    """Verifica se há missões em data/claude_inbox pendentes"""
    mission_files = glob.glob(os.path.join(INBOX_DIR, "*.json"))
    for mf in mission_files:
        try:
            with open(mf, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data.get("status") == "dispatched":
                m_id = data.get("mission_id")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Nova missão detectada do Antigravity: {m_id}")
                print(f"Objetivo: {data.get('objective')}")

                # Invoca o Claude Code com o prompt da missão
                prompt = (
                    f"MISSÃO DO ANTIGRAVITY #{m_id}:\n"
                    f"{data.get('objective')}\n"
                    f"Instruções: {data.get('instructions')}\n"
                    f"Arquivos: {data.get('target_files')}"
                )

                claude_cmd = f'claude -p "{prompt}" --dangerously-skip-permissions'
                print(f"Executando Claude Code...")
                c_code, c_out, c_err = run_cmd(claude_cmd)

                # Gera relatório
                rep_path = os.path.join(OUTBOX_DIR, f"report_{m_id}.json")
                report = {
                    "mission_id": m_id,
                    "finished_at": datetime.utcnow().isoformat(),
                    "success": (c_code == 0),
                    "log": c_out[:2000]
                }
                with open(rep_path, "w", encoding="utf-8") as rf:
                    json.dump(report, rf, indent=2, ensure_ascii=False)

                data["status"] = "completed"
                with open(mf, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

                # Commit em tempo real
                run_cmd("git add .")
                run_cmd(f'git commit -m "feat(claude): conclui missão {m_id} com sucesso"')
                run_cmd("git push origin main")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Missão {m_id} concluída e commitada no Git!")

        except Exception as e:
            print(f"Erro ao processar missão {mf}: {e}")

def main_loop(poll_interval: int = 10):
    print("=" * 65)
    print("🔄 ANTIGRAVITY <-> CLAUDE CODE REAL-TIME SYNC BRIDGE")
    print(f"Diretório atual: {os.path.abspath('.')}")
    print(f"Pasta local configurada: /home/ldks/Documents/work-claude/")
    print("=" * 65)
    print(f"Pressione Ctrl+C para encerrar.")

    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(OUTBOX_DIR, exist_ok=True)

    while True:
        try:
            sync_git()
            process_pending_missions()
        except KeyboardInterrupt:
            print("\nEncerrando bridge.")
            break
        except Exception as e:
            print(f"Erro no loop do bridge: {e}")
        time.sleep(poll_interval)

if __name__ == "__main__":
    main_loop()
