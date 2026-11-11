"""HTTP-доступ к опубликованным данным и командам Auction Engine."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from common.database.clickhouse.auction_storage import AuctionStorage
from common.dataclasses.auction_history import HistoryKey
from common.enums.auction import AuctionMode
from common.errors import (
    AuctionConfigurationError,
    CatalogError,
    ItemNotFoundError,
)
from common.errors.clickhouse import HistoryStorageError
from common.errors.auction_engine import EngineCapacityError
from common.models.auction_engine import (
    EngineStatus,
    MonitoringUpdate,
    ScannerCommand,
)
from ..models.requests.auction_analysis import AuctionAnalysisQuery
from ..models.responses.auction_analysis import (
    AuctionAnalysisResponse,
    AuctionVersionResponse,
)
from common.models.history_status import HistoryStatus
from common.engine_analysis import history_summary
from ..auction import catalog_item, enqueue, read_analysis, read_version

router = APIRouter(prefix="/api")
_HEADERS = {"Cache-Control": "no-store"}
_QUERY = Annotated[AuctionAnalysisQuery, Query()]
_ERRORS = (
    ItemNotFoundError, CatalogError, HistoryStorageError,
    AuctionConfigurationError, EngineCapacityError,
)


def _storage(request: Request) -> AuctionStorage:
    """Получить хранилище веб-процесса.

    Args:
        request: HTTP-запрос.

    Returns:
        Управляемое подключение к ClickHouse.
    """
    storage = getattr(request.app.state, "auction_storage", None)
    if storage is None:
        raise HistoryStorageError("ClickHouse не настроен.")
    return storage


def _error(error: Exception) -> HTTPException:
    """Преобразовать ошибку в безопасный HTTP-ответ.

    Args:
        error: Ошибка каталога, конфигурации или хранилища.

    Returns:
        HTTP-ошибка с сообщением для интерфейса.
    """
    if isinstance(error, ItemNotFoundError):
        status, code, message = 404, "item_not_found", str(error)
    elif isinstance(error, CatalogError):
        status, code = 500, "catalog_error"
        message = "Не удалось прочитать каталог предметов."
    elif isinstance(error, EngineCapacityError):
        status, code, message = 429, "engine_capacity", str(error)
    else:
        status, code, message = 503, "storage_unavailable", str(error)
    return HTTPException(status, detail={
        "code": code, "message": message, "retryable": status in (429, 503),
    }, headers=_HEADERS)


@router.get("/auction/analysis", response_model=AuctionAnalysisResponse)
async def auction_analysis(query: _QUERY, request: Request) -> JSONResponse:
    """Выдать опубликованный профиль или состояние ожидания.

    Args:
        query: Предмет, период и пользовательская стратегия.
        request: Входящий запрос FastAPI.

    Returns:
        Готовая аналитика или HTTP 202.
    """
    try:
        result = await read_analysis(_storage(request), query)
    except _ERRORS as error:
        raise _error(error) from None
    if result is None:
        return JSONResponse({
            "status": "pending", "item_id": query.item_id,
            "message": "Ожидаем публикацию снимка и выбранного расчёта.",
        }, status_code=202, headers={**_HEADERS, "Retry-After": "5"})
    return JSONResponse(result.model_dump(mode="json"), headers=_HEADERS)


@router.get("/auction/version")
async def auction_version(
    query: _QUERY, request: Request,
) -> AuctionVersionResponse:
    """Проверить версию без передачи полной аналитики.

    Args:
        query: Предмет и выбранный профиль.
        request: Входящий запрос.

    Returns:
        Версия, возраст и состояние истории.
    """
    try:
        return await read_version(_storage(request), query)
    except _ERRORS as error:
        raise _error(error) from None


@router.get("/auction/history/status")
async def history_status(
    query: _QUERY, request: Request,
) -> HistoryStatus:
    """Прочитать контрольную точку из ClickHouse.

    Args:
        query: Ключ предмета.
        request: Входящий запрос FastAPI.

    Returns:
        Покрытие истории и актуальность данных.
    """
    try:
        catalog_item(query.item_id)
        storage = _storage(request)
        async with storage.session() as (history, _):
            point = await history.get_checkpoint(
                HistoryKey(query.item_id, query.region, query.mode),
            )
        return history_summary(point, storage.settings)
    except _ERRORS as error:
        raise _error(error) from None


@router.post("/auction/refresh", status_code=202)
async def refresh(query: _QUERY, request: Request) -> ScannerCommand:
    """Поставить команду на приоритетное сканирование.

    Args:
        query: Ключ предмета и стратегия.
        request: Входящий запрос FastAPI.

    Returns:
        Принятая команда ядра.
    """
    try:
        catalog_item(query.item_id)
        async with _storage(request).session() as (_, db):
            key = HistoryKey(query.item_id, query.region, query.mode)
            return await enqueue(db, key, query, "scan", manual=True)
    except _ERRORS as error:
        raise _error(error) from None


@router.patch("/auction/monitoring", status_code=202)
async def monitoring(
    query: _QUERY, command: MonitoringUpdate, request: Request,
) -> ScannerCommand:
    """Поставить команду на изменение мониторинга.

    Args:
        query: Ключ предмета.
        command: Изменения мониторинга, интервала и приоритета.
        request: Входящий запрос FastAPI.

    Returns:
        Команда с идентификатором и временем сервера.
    """
    try:
        catalog_item(query.item_id)
        if query.mode is not AuctionMode.PRODUCTION:
            raise HTTPException(422, "Мониторинг доступен в production RU.")
        async with _storage(request).session() as (_, db):
            key = HistoryKey(query.item_id, query.region, query.mode)
            return await enqueue(
                db, key, query, "settings", settings_update=command,
            )
    except _ERRORS as error:
        raise _error(error) from None


@router.get("/auction/commands/{request_id}")
async def command_status(
    request_id: str, query: _QUERY, request: Request,
) -> dict[str, str | None]:
    """Прочитать результат команды из ClickHouse.

    Args:
        request_id: Идентификатор запроса к ядру.
        query: Ключ предмета.
        request: Входящий запрос FastAPI.

    Returns:
        Состояние обработки и код ошибки.
    """
    try:
        async with _storage(request).session() as (_, db):
            key = HistoryKey(query.item_id, query.region, query.mode)
            result = await db.command_result(key, request_id)
        if result is None:
            raise HTTPException(404, "Команда не найдена или истекла.")
        return result
    except _ERRORS as error:
        raise _error(error) from None


@router.get("/engine/status")
async def engine_status(request: Request) -> dict:
    """Показать состояние ядра по последнему подтверждению активности.

    Args:
        request: Входящий запрос FastAPI.

    Returns:
        Метаданные планировщика и доступность базы.
    """
    try:
        storage = _storage(request)
        async with storage.session() as (_, db):
            raw = await db.latest_state(
                "scanner_state", HistoryKey("", "RU", AuctionMode.PRODUCTION),
            )
    except HistoryStorageError:
        return {"running": None, "clickhouse_available": False}
    if raw is None:
        return {"running": False, "clickhouse_available": True}
    state = EngineStatus.model_validate_json(raw)
    age = datetime.now(UTC) - state.heartbeat
    return {
        **state.model_dump(mode="json"), "clickhouse_available": True,
        "running": state.online and age < timedelta(
            seconds=max(15, storage.settings.scheduler_interval * 3),
        ),
    }
