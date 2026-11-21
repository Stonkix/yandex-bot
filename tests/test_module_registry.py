"""Проверки реестра подключаемых модулей."""

import asyncio
import unittest

from yandexbot.core.module_registry import BotModule, CommandContext, CommandSpec, ModuleRegistry, SettingField


class ModuleRegistryTests(unittest.TestCase):
    def test_dispatches_registered_command_to_its_handler(self):
        called = []

        async def handler(context):
            called.append((context.display_name, context.text))

        registry = ModuleRegistry()
        registry.register(CommandSpec(
            module_code="demo",
            commands=frozenset({"/демо"}),
            allowed=lambda _context: True,
            handler=handler,
        ))
        context = CommandContext({}, {}, None, "/демо", "Администратор")

        self.assertTrue(asyncio.run(registry.dispatch(context)))
        self.assertEqual(called, [("Администратор", "/демо")])
        self.assertFalse(asyncio.run(registry.dispatch(
            CommandContext({}, {}, None, "/нет", "Администратор"),
        )))

    def test_rejects_duplicate_command(self):
        registry = ModuleRegistry()

        async def handler(_context):
            return None

        spec = CommandSpec("one", frozenset({"/общая"}), lambda _context: True, handler)
        registry.register(spec)
        with self.assertRaisesRegex(ValueError, "зарегистрирована"):
            registry.register(CommandSpec(
                "two", frozenset({"/общая"}), lambda _context: True, handler,
            ))

    def test_registers_module_with_declarative_settings_schema(self):
        async def handler(_context):
            return None
        spec = CommandSpec("demo", frozenset({"/демо"}), lambda _context: True, handler)
        module = BotModule("demo", "Демо", frozenset({"/демо"}), spec,
                           (SettingField("enabled", "Включён", "checkbox", True),))
        registry = ModuleRegistry()
        registry.register_module(module)
        self.assertEqual(registry.modules()[0].settings_schema[0].key, "enabled")


if __name__ == "__main__":
    unittest.main()
