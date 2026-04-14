def executes_operation_in_background(func):
    """Mark a FastAPI endpoint as one that executes an operation in the background.

    Does nothing at runtime beyond setting the ``_executes_operation_in_background`` attribute;
    acts as a marker for documentation and introspection.
    """
    func._executes_operation_in_background = True
    return func
