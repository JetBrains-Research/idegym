def executes_operation_in_background(func):
    """
    Decorator to mark FastAPI endpoints as an endpoint that executes an operation in the background.
    Currently, does nothing but serves as documentation/marker.
    """
    func._executes_operation_in_background = True
    return func
