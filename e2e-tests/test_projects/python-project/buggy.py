"""File with intentional unresolved references for inspection testing."""


def use_undefined():
    """Function that uses undefined variables to trigger PyUnresolvedReferences."""
    print(undefined_variable)
    result = another_undefined_function()
    return missing_module.some_attribute
