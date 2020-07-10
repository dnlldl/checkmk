#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""Checker to prevent disallowed imports of modules

See chapter "Module hierarchy" in coding_guidelines_python in wiki
for further information.
"""

import os
from typing import NewType

from astroid.node_classes import Statement, Import, ImportFrom  # type: ignore[import]
from pylint.checkers import BaseChecker, utils  # type: ignore[import]
from pylint.interfaces import IAstroidChecker  # type: ignore[import]
from testlib import cmk_path

ModuleName = NewType('ModuleName', str)
ModulePath = NewType('ModulePath', str)  # TODO: use pathlib.Path
Component = NewType('Component', str)


def register(linter):
    linter.register_checker(CMKModuleLayerChecker(linter))


# https://www.python.org/dev/peps/pep-0616/
def removeprefix(text: str, prefix: str) -> str:
    return text[len(prefix):] if text.startswith(prefix) else text


_COMPONENTS = (
    Component("cmk.base"),
    Component("cmk.fetchers"),
    Component("cmk.snmplib"),
    Component("cmk.gui"),
    Component("cmk.ec"),
    Component("cmk.notification_plugins"),
    Component("cmk.special_agents"),
    Component("cmk.update_config"),
    Component("cmk.cee.dcd"),
    Component("cmk.cee.mknotifyd"),
    Component("cmk.cee.snmp_backend"),
    Component("cmk.cee.liveproxy"),
    Component("cmk.cee.notification_plugins"),
)

_EXPLICIT_FILE_TO_COMPONENT = {
    ModulePath("web/app/index.wsgi"): Component("cmk.gui"),
    ModulePath("bin/update_rrd_fs_names.py"): Component("cmk.base"),
    ModulePath("bin/check_mk"): Component("cmk.base"),
    ModulePath("bin/cmk-update-config"): Component("cmk.update_config"),
    ModulePath("bin/mkeventd"): Component("cmk.ec"),
    ModulePath("enterprise/bin/liveproxyd"): Component("cmk.cee.liveproxy"),
    ModulePath("enterprise/bin/mknotifyd"): Component("cmk.cee.mknotifyd"),
    ModulePath("enterprise/bin/dcd"): Component("cmk.cee.dcd"),
    # CEE specific notification plugins
    ModulePath("notifications/servicenow"): Component("cmk.cee.notification_plugins"),
    ModulePath("notifications/jira_issues"): Component("cmk.cee.notification_plugins"),
}


class CMKModuleLayerChecker(BaseChecker):
    __implements__ = IAstroidChecker

    name = 'cmk-module-layer-violation'
    msgs = {
        'C8410': ('Import of %r not allowed in module %r', 'cmk-module-layer-violation', 'whoop?'),
    }

    # This doesn't change during a pylint run, so let's save a realpath() call per import.
    path_prefix_len_to_strip = len(cmk_path()) + 1

    @utils.check_messages('cmk-module-layer-violation')
    def visit_import(self, node: Import) -> None:
        for name, _ in node.names:
            self._check_import(node, ModuleName(name))

    @utils.check_messages('cmk-module-layer-violation')
    def visit_importfrom(self, node: ImportFrom) -> None:
        self._check_import(node, ModuleName(node.modname))

    def _check_import(self, node: Statement, imported: ModuleName) -> None:
        if not imported.startswith("cmk"):
            return  # We only care about our own modules, ignore this

        # We use paths relative to our project root only.
        absolute_path: str = node.root().file
        importing_path = ModulePath(absolute_path[self.path_prefix_len_to_strip:])

        if importing_path.startswith("tests/"):
            return  # No validation in tests

        # Pylint fails to detect the correct module path here. Instead of realizing that the file
        # cmk/base/automations/cee.py is cmk.base.automations.cee it thinks the module is "cee".
        # We can silently ignore these files because the linked files at enterprise/... are checked.
        if os.path.islink(importing_path):
            return  # Ignore symlinked files instead of checking them twice, ignore this

        importing = self._get_module_name_of_file(node, importing_path)

        if not self._is_import_allowed(importing_path, importing, imported):
            self.add_message("cmk-module-layer-violation", node=node, args=(imported, importing))

    def _get_module_name_of_file(self, node: Statement, importing_path: ModulePath) -> ModuleName:
        """Fixup module names"""
        # Emacs' flycheck stores files to be checked in a temporary file with a prefix.
        module_name = removeprefix(node.root().name, "flycheck_")

        for segments in [
            ("cmk", "base", "plugins", "agent_based", "utils", ""),
            ("cmk", "base", "plugins", "agent_based", ""),
        ]:
            if importing_path.startswith('/'.join(segments)):
                return ModuleName('.'.join(segments) + module_name)

        # Fixup managed and enterprise module names
        # astroid does not produce correct module names, because it does not know
        # that we link/copy our CEE/CME parts to the cmk.* module in the site.
        # Fake the final module name here.
        for component in ["base", "gui"]:
            for prefix in [
                    "cmk/%s/cee/" % component,
                    "cmk/%s/cme/" % component,
                    "enterprise/cmk/%s/cee/" % component,
                    "managed/cmk/%s/cme/" % component,
            ]:
                if importing_path.startswith(prefix):
                    return ModuleName("cmk.%s.%s" % (component, module_name))

        if module_name.startswith("cee.") or module_name.startswith("cme."):
            return ModuleName("cmk.%s" % module_name)
        return ModuleName(module_name)

    def _is_import_allowed(self, importing_path: ModulePath, importing: ModuleName,
                           imported: ModuleName) -> bool:
        for component in _COMPONENTS:
            if not self._is_part_of_component(importing, importing_path, component):
                continue

            if self._is_disallowed_snmplib_import(importing, component):
                return True

            if self._is_disallowed_fetchers_import(importing, component):
                return True

            if self._is_import_in_component(imported, component):
                return True

            if self._is_import_in_cee_component_part(importing, imported, component):
                return True

        return self._is_utility_import(imported)

    def _is_part_of_component(self, importing: ModuleName, importing_path: ModulePath,
                              component: Component) -> bool:
        if self._is_import_in_component(importing, component):
            return True

        explicit_component = _EXPLICIT_FILE_TO_COMPONENT.get(importing_path)
        if explicit_component is not None:
            return explicit_component == component

        # The check and bakery plugins are all compiled together by tests/pylint/test_pylint.py.
        # They clearly belong to the cmk.base component.
        if component == Component("cmk.base") and importing.startswith("cmk_pylint"):
            return True

        if component == Component("cmk.notification_plugins") and importing_path.startswith(
                "notifications/"):
            return True

        if component == Component("cmk.special_agents") and importing_path.startswith(
                "agents/special/"):
            return True

        return False

    def _is_disallowed_fetchers_import(self, importing: ModuleName, component: Component) -> bool:
        """Disallow import of `fetchers` in `cmk.utils`.

        The layering is such that `fetchers` is between `utils` and
        `base` so that importing `fetchers` in `utils` is wrong but
        anywhere else is OK.

        """
        return not (component.startswith("cmk.fetchers") and importing.startswith("cmk.utils"))

    def _is_disallowed_snmplib_import(self, importing: ModuleName, component: Component) -> bool:
        """Disallow import of `snmplib` in `cmk.utils`."""
        return not component.startswith("cmk.snmplib") and importing.startswith("cmk.utils")

    def _is_import_in_component(self, imported: ModuleName, component: Component) -> bool:
        return imported == ModuleName(component) or imported.startswith(component + ".")

    def _is_import_in_cee_component_part(self, importing: ModuleName, imported: ModuleName,
                                         component: Component) -> bool:
        """If a module is split into cmk.cee.[mod] and cmk.[mod] it's allowed
        to import non-cee parts in the cee part."""
        return importing.startswith("cmk.cee.") and self._is_import_in_component(
            imported, component)

    def _is_utility_import(self, imported: ModuleName) -> bool:
        """cmk and cmk.utils are allowed to be imported from all over the place"""
        return imported in {"cmk", "cmk.utils"} or imported.startswith("cmk.utils.")