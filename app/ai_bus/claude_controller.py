"""
CLAUDE CODE CONTROLLER & ORCHESTRATION BRIDGE
Permite que o Antigravity (ou o sistema central) comande e manipule
o Claude Code em tempo real para editar código, criar testes, otimizar
funções e reportar resultados com commits automáticos em tempo real.
"""

import os
import sys
import json
import subprocess
import datetime
from typing import Dict, Any, List, Optional
from app.ai_bus.bus import AIBus

INBOX_DIR = "./data/claude_inbox"
OUTBOX_DIR = "./data/claude_outbox"

class ClaudeController:
    def __init__(self, repo_dir: str = "."):
        self.repo_dir = repo_dir
        os.makedirs(INBOX_DIR, exist_ok=True)
        os.makedirs(OUTBOX_DIR, exist_ok=True)

    def dispatch_mission(
        self,
        mission_id: str,
        objective: str,
        target_files: List[str],
        instructions: str,
        run_headless: bool = True
    ) -> Dict[str, Any]:
        """
        Envia uma missão direta para o Claude Code executar.
        Se run_headless=True, executa diretamente via CLI do Claude Code.
        Também registra a missão em data/claude_inbox/ para o daemon local.
        """
        mission_data = {
            "mission_id": mission_id,
            "created_at": datetime.datetime.utcnow().isoformat(),
            "objective": objective,
            "target_files": target_files,
            "instructions": instructions,
            "status": "dispatched"
        }

        # 1. Salva arquivo de missão no inbox
        inbox_file = os.path.join(INBOX_DIR, f"{mission_id}.json")
        with open(inbox_file, "w", encoding="utf-8") as f:
            json.dump(mission_data, f, indent=2, ensure_ascii=False)

        # 2. Publica no barramento de eventos
        with AIBus() as bus:
            bus.publish_event(
                sender_agent="antigravity",
                target_agent="claude-code",
                event_type="MISSION_ASSIGNED",
                payload=mission_data
            )

        # 3. Se run_headless=True e claude estiver instalado, executa diretamente
        execution_output = ""
        success = True

        if run_headless:
            prompt = (
                f"### MISSÃO DO ANTIGRAVITY #{mission_id} ###\n\n"
                f"OBJETIVO: {objective}\n"
                f"ARQUIVOS ALVO: {', '.join(target_files)}\n\n"
                f"INSTRUÇÕES:\n{instructions}\n\n"
                f"Após concluir as alterações, valide a sintaxe dos arquivos modificados."
            )

            cmd = [
                "claude",
                "-p", prompt,
                "--dangerously-skip-permissions"
            ]

            try:
                res = subprocess.run(cmd, cwd=self.repo_dir, capture_output=True, text=True, timeout=600)
                execution_output = res.stdout + ("\n" + res.stderr if res.stderr else "")
                success = (res.returncode == 0)
            except Exception as e:
                execution_output = f"Erro ao invocar Claude Code CLI: {str(e)}"
                success = False

        # 4. Salva relatório no outbox
        outbox_file = os.path.join(OUTBOX_DIR, f"report_{mission_id}.json")
        report = {
            "mission_id": mission_id,
            "completed_at": datetime.datetime.utcnow().isoformat(),
            "success": success,
            "output": execution_output[:4000]
        }
        with open(outbox_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        return report

    def git_commit_and_push(self, message: str) -> Dict[str, Any]:
        """
        Realiza commit em tempo real e push para o repositório compartilhado,
        permitindo que o Claude Code em qualquer máquina receba as alterações.
        """
        try:
            subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
            res = subprocess.run(["git", "commit", "-m", message], cwd=self.repo_dir, capture_output=True, text=True)
            
            # Tenta push se houver remote configurado
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=self.repo_dir, capture_output=True, text=True)
            
            return {
                "success": True,
                "commit_output": res.stdout,
                "push_output": push_res.stdout or push_res.stderr
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

if __name__ == "__main__":
    controller = ClaudeController()
    if len(sys.argv) > 1 and sys.argv[1] == "dispatch":
        res = controller.dispatch_mission(
            mission_id=f"task_{int(datetime.datetime.now().timestamp())}",
            objective="Verificar integridade do sistema",
            target_files=["app/main.py"],
            instructions="Verifique se todos os endpoints do FastAPI estão funcionando e responda com OK."
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
