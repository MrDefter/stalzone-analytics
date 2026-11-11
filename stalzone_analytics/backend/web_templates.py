"""Настройка шаблонов демонстрационного веб-интерфейса."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "frontend"
templates = Jinja2Templates(directory=FRONTEND_ROOT / "templates")
