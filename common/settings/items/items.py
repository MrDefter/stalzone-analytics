"""Чтение и проверка списка предметов из items.json."""

from pathlib import Path
from typing import Self

from pydantic import ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from common.dataclasses import CatalogItem
from common.errors import CatalogError

_ITEMS_FILE = Path(__file__).resolve().with_name("items.json")


class ItemCatalogSettings(BaseSettings):
    """Настройки локального списка предметов."""

    model_config = SettingsConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )

    items: tuple[CatalogItem, ...]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Использовать данные, переданные загрузчиком JSON.

        Args:
            settings_cls: Класс создаваемых настроек.
            init_settings: Источник аргументов конструктора.
            env_settings: Источник переменных окружения.
            dotenv_settings: Источник значений из .env.
            file_secret_settings: Источник файлов секретов.

        Returns:
            Кортеж с источником аргументов конструктора.
        """
        return (init_settings,)

    @field_validator("items")
    @classmethod
    def _validate_items(
        cls, items: tuple[CatalogItem, ...]
    ) -> tuple[CatalogItem, ...]:
        """Проверить имена и уникальность точных ID.

        Args:
            items: Предметы, прочитанные из конфигурации.

        Returns:
            Исходные записи после проверки.

        Raises:
            ValueError: ID или имя недопустимы либо ID повторяется.
        """
        seen = set()
        for item in items:
            if not item.item_id.strip() or item.item_id in (".", ".."):
                raise ValueError("у предмета недопустимый item_id")
            if item.name_ru is None or not item.name_ru.strip():
                raise ValueError(f"у предмета {item.item_id!r} пустое имя")
            if item.item_id in seen:
                raise ValueError(f"повтор ID {item.item_id!r}")
            seen.add(item.item_id)
        return items

    @classmethod
    def load(
        cls, path: Path | None = None, *, allow_empty: bool = False
    ) -> Self:
        """Загрузить список предметов через источник JSON.

        Args:
            path: Путь к своему JSON или None для файла рядом с модулем.
            allow_empty: Разрешить каталог без предметов.

        Returns:
            Проверенные настройки со списком предметов.

        Raises:
            CatalogError: Файл недоступен или нарушает схему настроек.
        """
        file_path = (path or _ITEMS_FILE).absolute()
        try:
            if not file_path.is_file():
                raise CatalogError(
                    f"Файл списка предметов отсутствует по пути {file_path}."
                )
            source = JsonConfigSettingsSource(
                cls, json_file=file_path, json_file_encoding="utf-8-sig"
            )
            settings = cls(**source())
            if not settings.items and not allow_empty:
                raise CatalogError(
                    f"Некорректное поле items в файле {file_path} "
                    "(список предметов пуст)."
                )
            return settings
        except ValidationError as exc:
            error = exc.errors(
                include_input=False, include_context=False, include_url=False
            )[0]
            field = ".".join(str(part) for part in error["loc"])
            raise CatalogError(
                f"Некорректное поле {field} в файле {file_path} "
                f"({error['msg']})."
            ) from None
        except OSError:
            raise CatalogError(
                f"Не удалось прочитать файл {file_path}."
            ) from None
        except (ValueError, TypeError, RecursionError):
            raise CatalogError(
                f"Некорректный JSON в файле {file_path}."
            ) from None
