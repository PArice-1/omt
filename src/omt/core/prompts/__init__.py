"""加载随 OMT 分发的基础提示词；不读取工作目录或用户配置中的同名文件。"""

from importlib.resources import files


def load_system_prompt() -> str:
    """读取内置 UTF-8 提示词；缺失、编码错误或空内容均阻止启动。"""
    prompt = files("omt.core.prompts").joinpath("system.md").read_text(
        encoding="utf-8"
    )
    if not prompt.strip():
        raise ValueError("内置系统提示词不能为空")
    return prompt
