# -*- coding: utf-8 -*-
"""pytest 公共配置：确保项目根目录在 sys.path，便于导入 devices.* 包。"""

import os
import sys

_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
# control_service/tests -> control_service -> adiclockevals -> generators -> devices -> 根
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
