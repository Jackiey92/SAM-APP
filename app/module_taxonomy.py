"""兼容旧导入路径，新代码请用 app.module_tree；勿在此加逻辑。"""
from .module_tree import (Module, MODULES, MODULE_KEYS, TODO_DIRECTORY,
                          UNCLASSIFIED, validated_module, projection_module,
                          module_definitions)
