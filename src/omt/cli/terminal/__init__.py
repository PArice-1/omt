"""终端展示入口；普通终端函数保留原导入路径，Textual 界面按需加载。"""

from .plain import (
    InputValidator as InputValidator,
    display_history as display_history,
    display_thinking as display_thinking,
    format_output as format_output,
    print_event as print_event,
    print_warnings as print_warnings,
    read_input as read_input,
    read_input_sync as read_input_sync,
)
