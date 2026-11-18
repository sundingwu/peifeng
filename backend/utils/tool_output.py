"""保留文本兼容性的工具回执，显式携带批次失败状态。"""


class ToolOutput(str):
    def __new__(cls, text: str, has_errors: bool = False):
        output = super().__new__(cls, text)
        output.has_errors = has_errors
        return output

    def __getnewargs__(self):
        return str(self), self.has_errors
