```markdown
# omnigent Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches the core development patterns and conventions used in the `omnigent` repository, a Python backend built with the Flask framework. You'll learn how to structure files, write imports and exports, and follow the project's coding and testing standards. This guide also provides suggested commands for common workflows to streamline your development process.

## Coding Conventions

### File Naming
- Use **snake_case** for all file names.
  - Example: `user_routes.py`, `data_utils.py`

### Import Style
- Use **alias imports** to clarify module usage and avoid naming conflicts.
  - Example:
    ```python
    import flask as fk
    import numpy as np
    ```

### Export Style
- Use **named exports** to explicitly control what is accessible from a module.
  - Example:
    ```python
    __all__ = ['MyClass', 'my_function']
    ```

### Commit Patterns
- Commit messages are freeform, sometimes with prefixes, and average 49 characters.
  - Example:
    ```
    Add endpoint for user authentication
    Fix: resolve issue with data serialization
    ```

## Workflows

### Adding a New Flask Route
**Trigger:** When you need to add a new API endpoint.
**Command:** `/add-route`

1. Create a new file in `snake_case` if grouping routes, or add to an existing routes file.
2. Import Flask and any required modules using aliases.
    ```python
    import flask as fk
    ```
3. Define your route using Flask's decorators.
    ```python
    @fk.route('/my-endpoint', methods=['GET'])
    def my_endpoint():
        return fk.jsonify({'message': 'Hello!'})
    ```
4. Add the route function to `__all__` if exporting.
5. Commit your changes with a concise, descriptive message.

### Creating a Utility Module
**Trigger:** When you need reusable helper functions.
**Command:** `/add-utility`

1. Create a new file with a snake_case name (e.g., `string_utils.py`).
2. Write your utility functions.
    ```python
    def capitalize_words(text):
        return ' '.join(word.capitalize() for word in text.split())
    ```
3. Add function names to `__all__` for named exports.
    ```python
    __all__ = ['capitalize_words']
    ```
4. Import this module using an alias where needed.
    ```python
    import string_utils as su
    ```
5. Commit your changes.

## Testing Patterns

- **Framework:** `vitest` (JavaScript/TypeScript)
- **File Pattern:** All test files are named with the `.test.tsx` suffix.
    - Example: `user_routes.test.tsx`
- Tests are written in TypeScript/React style, not Python.
- Place test files alongside or near the modules they test.

## Commands

| Command       | Purpose                                      |
|---------------|----------------------------------------------|
| /add-route    | Scaffold and add a new Flask API route       |
| /add-utility  | Create a new utility module with exports     |

```