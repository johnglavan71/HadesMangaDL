# app/utils.py

def sanitize_filename(name: str) -> str:
    """
    Removes characters from a string that are not safe for filenames.

    This function ensures that chapter names or series titles can be safely used
    to create directories and files without causing errors on the operating system.

    Args:
        name: The input string, typically a series or chapter title.

    Returns:
        A sanitized string suitable for use as part of a file path.
    """
    if not isinstance(name, str):
        return ""
    
    # Allows alphanumeric characters, spaces, periods, underscores, and hyphens.
    return "".join(c for c in name if c.isalnum() or c in (' ', '.', '_', '-')).strip()