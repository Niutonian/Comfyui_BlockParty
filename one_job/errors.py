"""Step results the launcher branches on. A log line is not a control path."""


class StepError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ComfyUI reports these as execution text. The match is a rule, not a model.
OOM_PATTERNS = (
    "cuda out of memory",
    "hip out of memory",
    "out of memory",
    "outofmemoryerror",
    "allocation on device",
)


def classify_execution(text: str) -> str:
    folded = text.lower()
    for pattern in OOM_PATTERNS:
        if pattern in folded:
            return "oom"
    return "unclassified"
