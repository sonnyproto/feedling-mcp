"""World book domain."""


def register(app):
    from worldbook import routes

    app.register_blueprint(routes.bp)
