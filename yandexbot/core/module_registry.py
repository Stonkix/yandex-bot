"""Реестр подключаемых модулей и их команд.

Модуль регистрирует команды один раз; главный цикл бота больше не знает,
какая функция стоит за конкретной командой.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandContext:
    """Контекст одного сообщения Яндекс Мессенджера."""

    chat: dict
    sender: dict
    target: dict | None
    text: str
    display_name: str
    update_id: int | None = None


CommandHandler = Callable[[CommandContext], Awaitable[None]]
CommandAllowed = Callable[[CommandContext], bool]


@dataclass(frozen=True)
class CommandSpec:
    module_code: str
    commands: frozenset[str]
    allowed: CommandAllowed
    handler: CommandHandler


@dataclass(frozen=True)
class SettingField:
    """Декларативное поле формы настроек модуля."""

    key: str
    label: str
    kind: str
    default: object
    minimum: float | None = None


@dataclass(frozen=True)
class BotModule:
    """Единый внешний интерфейс подключаемого модуля."""

    code: str
    title: str
    commands: frozenset[str]
    command: CommandSpec
    settings_schema: tuple[SettingField, ...] = ()


class ModuleRegistry:
    """Глубокий модуль: поиск команды, проверка доступа и запуск обработчика."""

    def __init__(self):
        self._by_command: dict[str, CommandSpec] = {}
        self._modules: dict[str, BotModule] = {}

    def register(self, spec: CommandSpec) -> None:
        for command in spec.commands:
            normalized = command.lower()
            if normalized in self._by_command:
                raise ValueError(f"Команда уже зарегистрирована: {normalized}")
            self._by_command[normalized] = spec

    def register_module(self, module: BotModule) -> None:
        if module.code in self._modules:
            raise ValueError(f"Модуль уже зарегистрирован: {module.code}")
        self.register(module.command)
        self._modules[module.code] = module

    def commands(self) -> set[str]:
        return set(self._by_command)

    def modules(self) -> list[BotModule]:
        return list(self._modules.values())

    async def dispatch(self, context: CommandContext) -> bool:
        """Возвращает True, если команда принадлежит зарегистрированному модулю."""
        spec = self._by_command.get(context.text)
        if spec is None:
            return False
        if not spec.allowed(context):
            await spec.handler(context)
            return True
        await spec.handler(context)
        return True
