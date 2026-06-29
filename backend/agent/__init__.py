"""Agent-facing backend verbs for resident CLI tools."""


def register(app):
    from agent import routes

    app.register_blueprint(routes.bp)
