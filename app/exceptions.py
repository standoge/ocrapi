class AppError(Exception):
    """Base application error."""

    def __init__(
        self,
        title: str,
        detail: str,
        status: int = 500,
        type_: str = "about:blank",
    ) -> None:
        self.title = title
        self.detail = detail
        self.status = status
        self.type_ = type_
        super().__init__(detail)


class ValidationError(AppError):
    def __init__(self, detail: str, instance: str | None = None) -> None:
        super().__init__("Bad Request", detail, status=400)
        self.instance = instance


class PayloadTooLargeError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__("Payload Too Large", detail, status=413)


class UnprocessablePdfError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__("Unprocessable Entity", detail, status=422)


class UpstreamServiceError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__("Bad Gateway", detail, status=502)


class ConfigurationError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__("Internal Server Error", detail, status=500)


class NotFoundError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__("Not Found", detail, status=404)
