__all__ = ["process_user_message"]


def __getattr__(name):
    """Carga el núcleo conversacional solo cuando alguien lo utiliza.

    Importar cualquier submódulo liviano como ``chatbot.storage`` no necesita
    inicializar RAG/ML ni el cliente conversacional completo. ``from chatbot
    import process_user_message`` conserva la misma API y activa el import
    únicamente en ese punto.
    """
    if name == "process_user_message":
        from .core import process_user_message

        return process_user_message
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
