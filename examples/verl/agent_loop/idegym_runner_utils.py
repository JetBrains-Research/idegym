from dataclasses import dataclass, field


@dataclass
class ItemToRun:
    idx: int
    dp_id: str
    file_path: str
    replace_content: str
    method_name: str
    start_line: int
    end_line: int
    tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert the ItemToRun instance to a dictionary for JSON serialization."""
        return {
            "idx": self.idx,
            "dp_id": self.dp_id,
            "file_path": self.file_path,
            "replace_content": self.replace_content,
            "method_name": self.method_name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "tests": self.tests,
        }

    @classmethod
    def from_item(
        cls,
        item: dict,
        replace: str = "",
        startline: int | None = None,
        endline: int | None = None,
        tests: list[str] | None = None,
        max_num_tests: int = -1,
    ):
        """Factory method to create ItemToRun from an item dict."""

        if tests is None:
            tests = item["tests"]["full_paths"]
        if tests and max_num_tests > 0:
            tests = tests[:max_num_tests]

        method_dec = item["method"]["declaration"]
        declaration_indent = (len(method_dec) - len(method_dec.lstrip(" "))) * " "
        # Generated code will starts with method declaration
        if replace:
            code_lines = replace.splitlines()
            method_dec = code_lines[0]
            if (len(method_dec) - len(method_dec.lstrip(" "))) == 0:
                replace = "\n".join(declaration_indent + line for line in code_lines)

        if startline is None:
            startline = item["method"]["global_method_declaration_index"][0] + 1
        if endline is None:
            endline = item["method"]["global_method_body_index"][1] + 1

        return cls(
            idx=item["idx"],
            dp_id=item["dp_id"],
            file_path=item["file_path"],
            replace_content=replace,
            method_name=item["method"]["name"],
            start_line=startline,
            end_line=endline,
            tests=tests,
        )
