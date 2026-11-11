"""Маршруты демонстрационного интерфейса."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from stalzone_analytics.backend.web_templates import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Отдать главную страницу демоинтерфейса.

    Args:
        request: Входящий HTTP-запрос для контекста шаблона.
    Returns:
        HTML-страница со ссылками на локальные ресурсы.
    """
    return templates.TemplateResponse(request=request, name="index.html")
