from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status_code: int, error: str, detail: str = "") -> None:
        super().__init__(detail or error)
        self.status_code = status_code
        self.error = error
        self.detail = detail


def _body(error: str, detail) -> dict:
    return {"error": error, "detail": detail}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(_body(exc.error, exc.detail), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(_body("validation_error", _errors(exc.errors())), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        error = "not_found" if exc.status_code == 404 else f"http_{exc.status_code}"
        return JSONResponse(_body(error, str(exc.detail)), status_code=exc.status_code)


def _errors(errors) -> list[dict]:
    return [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in errors
    ]
