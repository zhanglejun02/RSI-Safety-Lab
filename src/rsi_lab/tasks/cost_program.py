"""Bounded Python-shaped programs for the authorization-cost experiment.

This is a small AST interpreter, not Python execution or an OS sandbox. Model
source never reaches eval, exec, compile, imports, or arbitrary object methods.
The only external operations are four explicit, host-supplied data calls.
"""
import ast
from collections.abc import Callable

from rsi_lab.errors import ValidationError

MAX_SOURCE_BYTES = 16384
MAX_AST_NODES = 1000
MAX_STEPS = 4096
MAX_DATA_BYTES = 65536
MAX_ITEMS = 256
MAX_DEPTH = 16
MAX_INTEGER = 10**12
API_ARITY = {"ask": 1, "execute": 1, "version": 0, "now": 0}
BUILTINS = {"len", "str", "int", "bool", "list", "tuple", "range", "min", "max"}
_NODES = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign,
          ast.AugAssign, ast.Expr, ast.If, ast.For, ast.Return, ast.Pass,
          ast.Break, ast.Continue, ast.Name, ast.Constant, ast.List, ast.Tuple,
          ast.Dict, ast.Subscript, ast.Call, ast.Attribute, ast.BinOp, ast.UnaryOp,
          ast.BoolOp, ast.Compare, ast.IfExp, ast.Load, ast.Store, ast.Add, ast.Sub,
          ast.Not, ast.USub, ast.UAdd, ast.And, ast.Or, ast.Eq, ast.NotEq,
          ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot)


def _data(value, *, clone=False):
    """Check size/types/cycles without invoking user-defined Python methods."""
    budget, active = [MAX_DATA_BYTES, 4096], set()

    def visit(item, depth):
        budget[1] -= 1
        if depth > MAX_DEPTH or budget[1] < 0:
            raise ValidationError("Program data nesting/item budget exceeded")
        kind = type(item)
        if item is None or kind is bool:
            budget[0] -= 8
            out = item
        elif kind is int:
            if abs(item) > MAX_INTEGER:
                raise ValidationError("Program integer limit exceeded")
            budget[0] -= 16
            out = item
        elif kind is str:
            size = len(item.encode("utf-8"))
            if size > 8192:
                raise ValidationError("Program string limit exceeded")
            budget[0] -= size + 8
            out = item
        elif kind in (list, tuple, dict):
            if len(item) > MAX_ITEMS or id(item) in active:
                raise ValidationError("Program container is oversized or cyclic")
            active.add(id(item))
            budget[0] -= 8
            if kind is dict:
                if any(type(key) is not str for key in item):
                    raise ValidationError("Program dictionary keys must be strings")
                out = {visit(key, depth + 1): visit(val, depth + 1) for key, val in item.items()}
            else:
                values = [visit(val, depth + 1) for val in item]
                out = tuple(values) if kind is tuple else values
            active.remove(id(item))
        else:
            raise ValidationError("Program values must be plain bounded data")
        if budget[0] < 0:
            raise ValidationError("Program data byte budget exceeded")
        return out if clone else item

    try:
        return visit(value, 0)
    except (UnicodeError, RecursionError) as exc:
        raise ValidationError("Invalid program data") from exc


def _parse(source):
    if type(source) is not str:
        raise ValidationError("Program source must be text")
    try:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValidationError("Program source limit exceeded")
        tree = ast.parse(source)
    except ValidationError:
        raise
    except (SyntaxError, UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("Invalid bounded program syntax") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES or any(type(node) not in _NODES for node in nodes):
        raise ValidationError("Unsupported or oversized bounded program")
    if len(tree.body) != 1 or type(tree.body[0]) is not ast.FunctionDef:
        raise ValidationError("Program must contain only handle(request, cache, api)")
    fn = tree.body[0]
    args = fn.args
    if (fn.name != "handle" or fn.decorator_list or fn.returns or fn.type_comment
            or getattr(fn, "type_params", []) or args.posonlyargs or args.kwonlyargs
            or args.vararg or args.kwarg or args.defaults or args.kw_defaults
            or [arg.arg for arg in args.args] != ["request", "cache", "api"]
            or any(arg.annotation or arg.type_comment for arg in args.args)
            or sum(type(node) is ast.FunctionDef for node in nodes) != 1):
        raise ValidationError("Program signature must be handle(request, cache, api)")
    parents = {id(child): node for node in nodes for child in ast.iter_child_nodes(node)}
    for node in nodes:
        if type(node) is ast.Name:
            if "__" in node.id or node.id.startswith("_") and node.id != "_":
                raise ValidationError("Private program names are forbidden")
            if isinstance(node.ctx, ast.Store) and node.id in BUILTINS | {"api", "handle"}:
                raise ValidationError("Program cannot rebind API or builtin names")
            if node.id == "api":
                parent = parents.get(id(node))
                if type(parent) is not ast.Attribute or parent.value is not node:
                    raise ValidationError("API is only available through explicit calls")
        elif type(node) is ast.Attribute:
            parent = parents.get(id(node))
            if type(parent) is not ast.Call or parent.func is not node:
                raise ValidationError("Attributes cannot be read or stored")
            is_api = type(node.value) is ast.Name and node.value.id == "api"
            if node.attr not in (API_ARITY if is_api else {"get", "append"}):
                raise ValidationError("Program method is not allowed")
        elif type(node) is ast.Call:
            if node.keywords or len(node.args) > 8:
                raise ValidationError("Calls require at most eight positional arguments")
            if type(node.func) is ast.Name:
                if node.func.id not in BUILTINS:
                    raise ValidationError("Only declared pure builtin calls are allowed")
            elif type(node.func) is not ast.Attribute:
                raise ValidationError("Dynamic program calls are forbidden")
        elif type(node) is ast.Constant:
            _data(node.value)
        elif type(node) in (ast.Break, ast.Continue):
            parent = parents.get(id(node))
            while parent is not None and type(parent) is not ast.For:
                parent = parents.get(id(parent))
            if parent is None:
                raise ValidationError("Loop control outside a loop")
    return fn


def validate_program(source: str) -> None:
    """Reject source outside the explicitly interpreted language."""
    _parse(source)


class _Return(Exception):
    pass


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _Machine:
    def __init__(self, request, cache, dispatch):
        self.env = {"request": _data(request, clone=True), "cache": _data(cache, clone=True)}
        _data(self.env)
        self.dispatch = dispatch
        self.steps = MAX_STEPS

    def tick(self):
        self.steps -= 1
        if self.steps < 0:
            raise ValidationError("Program instruction budget exceeded")

    def expression(self, node):
        self.tick()
        kind = type(node)
        if kind is ast.Constant:
            result = node.value
        elif kind is ast.Name:
            if node.id not in self.env:
                raise ValidationError("Program references an unbound name")
            result = self.env[node.id]
        elif kind in (ast.List, ast.Tuple):
            result = [self.expression(item) for item in node.elts]
            if kind is ast.Tuple:
                result = tuple(result)
        elif kind is ast.Dict:
            result = {self.expression(key): self.expression(value)
                      for key, value in zip(node.keys, node.values)}
        elif kind is ast.Subscript:
            target, key = self.expression(node.value), self.expression(node.slice)
            result = self.subscript(target, key)
        elif kind is ast.BinOp:
            result = self.binary(node.op, self.expression(node.left), self.expression(node.right))
        elif kind is ast.UnaryOp:
            operand = self.expression(node.operand)
            if type(node.op) is ast.Not:
                result = not operand
            elif type(operand) is int:
                result = -operand if type(node.op) is ast.USub else operand
            else:
                raise ValidationError("Unary arithmetic requires integers")
        elif kind is ast.BoolOp:
            result = None
            for value in node.values:
                result = self.expression(value)
                if type(node.op) is ast.And and not result or type(node.op) is ast.Or and result:
                    break
        elif kind is ast.Compare:
            left, result = self.expression(node.left), True
            for op, right_node in zip(node.ops, node.comparators):
                right = self.expression(right_node)
                result = self.compare(op, left, right)
                if not result:
                    break
                left = right
        elif kind is ast.IfExp:
            result = self.expression(node.body if self.expression(node.test) else node.orelse)
        elif kind is ast.Call:
            is_builtin = type(node.func) is ast.Name
            is_api = (not is_builtin and type(node.func.value) is ast.Name
                      and node.func.value.id == "api")
            # Match Python's receiver-before-arguments evaluation order, without
            # ever constructing a Python bound method or exposing the API object.
            target = None if is_builtin or is_api else self.expression(node.func.value)
            args = [self.expression(arg) for arg in node.args]
            if is_builtin:
                result = self.builtin(node.func.id, args)
            elif is_api:
                name = node.func.attr
                if len(args) != API_ARITY[name] or args and type(args[0]) is not dict:
                    raise ValidationError("Invalid program API arguments")
                result = _data(self.dispatch(name, _data(args, clone=True)), clone=True)
            else:
                if node.func.attr == "get" and type(target) is dict and len(args) in (1, 2):
                    if type(args[0]) is not str:
                        raise ValidationError("Dictionary lookup requires a string key")
                    result = target.get(*args)
                elif node.func.attr == "append" and type(target) is list and len(args) == 1:
                    if len(target) >= MAX_ITEMS:
                        raise ValidationError("Program list item limit exceeded")
                    target.append(args[0])
                    _data(target)
                    result = None
                else:
                    raise ValidationError("Invalid program method receiver or arguments")
        else:
            raise ValidationError("Unsupported program expression")
        return _data(result)

    @staticmethod
    def subscript(target, key):
        if type(target) is dict and type(key) is str:
            return target[key]
        if type(target) in (list, tuple, str) and type(key) is int:
            return target[key]
        raise ValidationError("Invalid program subscription")

    @staticmethod
    def binary(op, left, right):
        if type(op) is ast.Add and type(left) is type(right) and type(left) in (int, str, list, tuple):
            return _data(left + right)
        if type(op) is ast.Sub and type(left) is int and type(right) is int:
            return _data(left - right)
        raise ValidationError("Unsupported program arithmetic")

    @staticmethod
    def compare(op, left, right):
        kind = type(op)
        if kind is ast.Eq:
            return left == right
        if kind is ast.NotEq:
            return left != right
        if kind in (ast.Is, ast.IsNot):
            if left is not None and right is not None and type(left) is not bool and type(right) is not bool:
                raise ValidationError("Identity comparison is limited to None and bool")
            return (left is right) if kind is ast.Is else (left is not right)
        if kind in (ast.In, ast.NotIn):
            if type(right) not in (dict, list, tuple, str):
                raise ValidationError("Membership requires a container")
            return left in right if kind is ast.In else left not in right
        if type(left) is not type(right) or type(left) not in (int, str):
            raise ValidationError("Ordering requires two integers or strings")
        if kind is ast.Lt:
            return left < right
        if kind is ast.LtE:
            return left <= right
        if kind is ast.Gt:
            return left > right
        return left >= right

    @staticmethod
    def builtin(name, args):
        if name in ("len", "str", "int", "bool", "list", "tuple") and len(args) == 1:
            value = args[0]
            if name == "len" and type(value) in (str, dict, list, tuple):
                return len(value)
            if name == "str":
                return _data(str(value))
            if name == "int" and type(value) in (int, bool, str):
                if type(value) is str and len(value) > 16:
                    raise ValidationError("Integer conversion exceeds bound")
                return _data(int(value))
            if name == "bool":
                return bool(value)
            if name in ("list", "tuple") and type(value) in (list, tuple, dict, str):
                return _data(list(value) if name == "list" else tuple(value))
        if name in ("list", "tuple") and not args:
            return [] if name == "list" else ()
        if name == "range" and 1 <= len(args) <= 3 and all(type(arg) is int for arg in args):
            values = range(*args)
            if len(values) > MAX_ITEMS:
                raise ValidationError("Program range limit exceeded")
            return list(values)
        if name in ("min", "max") and args:
            values = args[0] if len(args) == 1 else args
            if (type(values) in (list, tuple) and values
                    and type(values[0]) in (int, str)
                    and all(type(value) is type(values[0]) for value in values)):
                return min(values) if name == "min" else max(values)
        raise ValidationError("Invalid bounded builtin arguments")

    def assign(self, target, value):
        self.tick()
        if type(target) is ast.Name:
            self.env[target.id] = value
        elif type(target) in (ast.Tuple, ast.List):
            if type(value) not in (list, tuple) or len(value) != len(target.elts):
                raise ValidationError("Invalid bounded unpacking")
            for item, val in zip(target.elts, value):
                self.assign(item, val)
        elif type(target) is ast.Subscript:
            container, key = self.expression(target.value), self.expression(target.slice)
            if type(container) is dict and type(key) is str:
                container[key] = value
            elif type(container) is list and type(key) is int:
                container[key] = value
            else:
                raise ValidationError("Invalid bounded assignment target")
        else:
            raise ValidationError("Unsupported assignment")
        _data(self.env)

    def block(self, statements):
        for node in statements:
            self.tick()
            kind = type(node)
            if kind is ast.Assign:
                value = self.expression(node.value)
                for target in node.targets:
                    self.assign(target, value)
            elif kind is ast.AugAssign:
                if type(node.target) is ast.Name:
                    left = self.expression(node.target)
                    self.assign(node.target, self.binary(node.op, left, self.expression(node.value)))
                elif type(node.target) is ast.Subscript:
                    container = self.expression(node.target.value)
                    key = self.expression(node.target.slice)
                    left = self.subscript(container, key)
                    value = self.binary(node.op, left, self.expression(node.value))
                    if type(container) is dict and type(key) is str or type(container) is list and type(key) is int:
                        container[key] = value
                        _data(self.env)
                    else:
                        raise ValidationError("Invalid bounded augmented assignment")
                else:
                    raise ValidationError("Invalid bounded augmented assignment target")
            elif kind is ast.Expr:
                self.expression(node.value)
            elif kind is ast.If:
                self.block(node.body if self.expression(node.test) else node.orelse)
            elif kind is ast.For:
                iterable = self.expression(node.iter)
                if type(iterable) not in (list, tuple, dict, str) or len(iterable) > MAX_ITEMS:
                    raise ValidationError("Invalid bounded loop iterable")
                for value in tuple(iterable):
                    self.assign(node.target, value)
                    try:
                        self.block(node.body)
                    except _Continue:
                        continue
                    except _Break:
                        break
                else:
                    self.block(node.orelse)
            elif kind is ast.Return:
                if node.value is not None:
                    self.expression(node.value)
                raise _Return()
            elif kind is ast.Break:
                raise _Break()
            elif kind is ast.Continue:
                raise _Continue()
            elif kind is not ast.Pass:
                raise ValidationError("Unsupported program statement")


def execute_program(source: str, request: dict, cache: dict,
                    dispatch: Callable[[str, list], object]) -> dict:
    """Run one request, returning a detached cache; API effects are not rolled back.

    Host dispatch receives detached plain arguments, and returned values are
    detached before use. A failure may follow an already executed API operation;
    callers must retain its operation log and mark execution incomplete.
    """
    if type(request) is not dict or type(cache) is not dict or not callable(dispatch):
        raise ValidationError("Execution requires request/cache dictionaries and a dispatcher")
    fn = _parse(source)
    machine = _Machine(request, cache, dispatch)
    try:
        try:
            machine.block(fn.body)
        except _Return:
            pass
        result = machine.env["cache"]
        if type(result) is not dict:
            raise ValidationError("Program cache must remain a dictionary")
        return _data(result, clone=True)
    except ValidationError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, RecursionError,
            _Break, _Continue) as exc:
        raise ValidationError("Bounded program execution failed: " + type(exc).__name__) from exc
