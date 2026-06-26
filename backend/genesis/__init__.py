"""Genesis import domain: chunked import ledger + CVM worker outputs."""


def register(app):
    from genesis import routes

    app.register_blueprint(routes.bp)
