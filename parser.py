"""Objects for the parsing phase of the compiler.

This parser is written entirely by hand because automatic parser generators are
no fun.

"""
from collections import namedtuple

import ctypes
import tree
import token_kinds
from errors import ParserError, error_collector
from il_gen import PointerCType, ArrayCType
from tokens import Token


class Parser:
    """Logic for converting a list of tokens into an AST.

    Each internal function parse_* corresponds to a unique non-terminal symbol
    in the C grammar. It parses self.tokens beginning at the given index to try
    to match a grammar rule that generates the desired symbol. If a match is
    found, it returns a tuple (Node, index) where Node is an AST node for that
    match and index is one more than that of the last token consumed in that
    parse. If no match is not found, raises an appropriate ParserError.

    Whenever a call to a parse_* function raises a ParserError, the calling
    function must either catch the exception and log it (using
    self._log_error), or pass the exception on to the caller. A function
    takes the first approach if there are other possible parse paths to
    consider, and the second approach if the function cannot parse the entity
    from the tokens.

    tokens (List(Token)) - The list of tokens to be parsed.
    best_error (ParserError) - The "best error" encountered thusfar. That is,
    out of all the errors encountered thusfar, this is the one that occurred
    after succesfully parsing the most tokens.

    """

    def __init__(self, tokens):
        """Initialize parser."""
        self.tokens = tokens
        self.best_error = None

    def parse(self):
        """Parse the provided list of tokens into an abstract syntax tree (AST).

        returns (Node) - Root node of the generated AST.

        """
        try:
            node, index = self.parse_main(0)
        except ParserError as e:
            self._log_error(e)
            error_collector.add(self.best_error)
            return None

        # Ensure there's no tokens left at after the main function
        if self.tokens[index:]:
            descrip = "unexpected token"
            error_collector.add(
                ParserError(descrip, index, self.tokens, ParserError.AT))

        return node

    def parse_main(self, index):
        """Parse a main function containing block items.

        Ex: int main() { return 4; }

        """
        err = "expected main function starting"
        index = self._match_token(index, token_kinds.int_kw, err,
                                  ParserError.AT)
        index = self._match_token(index, token_kinds.main, err, ParserError.AT)
        index = self._match_token(index, token_kinds.open_paren, err,
                                  ParserError.AT)
        index = self._match_token(index, token_kinds.close_paren, err,
                                  ParserError.AT)

        node, index = self.parse_compound_statement(index)
        return (tree.MainNode(node), index)

    def parse_statement(self, index):
        """Parse a statement.

        Try each possible type of statement, catching/logging exceptions upon
        parse failures. On the last try, raise the exception on to the caller.

        """
        try:
            return self.parse_compound_statement(index)
        except ParserError as e:
            self._log_error(e)

        try:
            return self.parse_return(index)
        except ParserError as e:
            self._log_error(e)

        try:
            return self.parse_if_statement(index)
        except ParserError as e:
            self._log_error(e)

        return self.parse_expr_statement(index)

    def parse_compound_statement(self, index):
        """Parse a compound statement.

        A compound statement is a collection of several
        statements/declarations, enclosed in braces.

        """
        index = self._match_token(index, token_kinds.open_brack,
                                  "expected '{'", ParserError.GOT)

        # Read block items (statements/declarations) until there are no more.
        nodes = []
        while True:
            try:
                node, index = self.parse_statement(index)
                nodes.append(node)
                continue
            except ParserError as e:
                self._log_error(e)

            try:
                node, index = self.parse_declaration(index)
                nodes.append(node)
                continue
            except ParserError as e:
                self._log_error(e)
                # When both of our parsing attempts fail, break out of the loop
                break

        index = self._match_token(index, token_kinds.close_brack,
                                  "expected '}'", ParserError.GOT)

        return (tree.CompoundNode(nodes), index)

    def parse_return(self, index):
        """Parse a return statement.

        Ex: return 5;

        """
        index = self._match_token(index, token_kinds.return_kw,
                                  "expected keyword 'return'", ParserError.GOT)
        return_kw = self.tokens[index - 1]
        node, index = self.parse_expression(index)
        index = self._expect_semicolon(index)
        return (tree.ReturnNode(node, return_kw), index)

    def parse_if_statement(self, index):
        """Parse an if statement."""
        index = self._match_token(index, token_kinds.if_kw,
                                  "expected keyword 'if'", ParserError.GOT)
        index = self._match_token(index, token_kinds.open_paren,
                                  "expected '('", ParserError.AFTER)
        conditional, index = self.parse_expression(index)
        index = self._match_token(index, token_kinds.close_paren,
                                  "expected ')'", ParserError.AFTER)
        statement, index = self.parse_statement(index)

        return (tree.IfStatementNode(conditional, statement), index)

    def parse_expr_statement(self, index):
        """Parse a statement that is an expression.

        Ex: a = 3 + 4

        """
        node, index = self.parse_expression(index)
        index = self._expect_semicolon(index)
        return (tree.ExprStatementNode(node), index)

    def match_function_call(self, stack):
        """Check if top of stack is a function call.

        On success, returns tuple (function name, [arguments]). On failure,
        returns None. The function name and arguments are each of type
        StackItem.

        """
        if len(stack) < 3: return None

        i = -1
        args = []

        # Expect top of stack to be `)`
        if (not isinstance(stack[i].item, Token) or
              stack[i].item.kind != token_kinds.close_paren):
            return None

        i -= 1

        # If next elements match ['EXPR', '('], we have a function with no
        # arguments.
        if (isinstance(stack[i].item, Token) and
            stack[i].item.kind == token_kinds.open_paren and
              isinstance(stack[i - 1].item, tree.Node)):
            return (stack[i - 1], args[::-1])

        while True:
            try:
                # Next element must be an expression.
                if isinstance(stack[i].item, tree.Node):
                    args.append(stack[i])
                else:
                    return None

                i -= 1

                # Next elements can be either a comma or ['EXPR', '(']
                if (isinstance(stack[i].item, Token) and
                      stack[i].item.kind == token_kinds.comma):
                    i -= 1
                elif (isinstance(stack[i].item, Token) and
                      stack[i].item.kind == token_kinds.open_paren and
                      isinstance(stack[i - 1].item, tree.Node)):
                    return (stack[i - 1], args[::-1])
                else:
                    return None

            except KeyError:
                return None

    def parse_expression(self, index):
        """Parse an expression.

        We parse expressions using a shift-reduce parser. We try to comprehend
        as much as possible of self.tokens past the index as being an
        expression, and the index returned is the first token that could not be
        parsed into the expression. If literally none of it could be parsed as
        an expression, raises an exception like usual.

        """
        # Dictionay of key-value pairs {TokenKind: precedence} where higher
        # precedence is higher.
        binary_operators = {token_kinds.plus: 11,
                            token_kinds.star: 12,
                            token_kinds.slash: 12,
                            token_kinds.twoequals: 8,
                            token_kinds.notequal: 8,
                            token_kinds.equals: 1}

        # Dictionary of unary prefix operators {TokenKind: tree.Node}
        unary_prefix_operators = {token_kinds.amp: tree.AddrOfNode,
                                  token_kinds.star: tree.DerefNode}

        # The set of assignment_tokens (because these are right-associative)
        assignment_operators = {token_kinds.equals}

        # An item in the parsing stack. The item is either a Node or Token,
        # where the node must generate an expression, and the length is the
        # number of tokens consumed in generating this node.
        StackItem = namedtuple("StackItem", ['item', 'length'])
        stack = []

        # TODO: clean up  the if-statements here
        i = index
        while True:
            # If the top of the stack is a number, reduce it to an expression
            # node
            if (stack and isinstance(stack[-1].item, Token) and
                    stack[-1].item.kind == token_kinds.number):
                stack[-1] = StackItem(tree.NumberNode(stack[-1].item), 1)

            # If the top of the stack is an identifier, reduce it to
            # an identifier node
            elif (stack and isinstance(stack[-1].item, Token) and
                  stack[-1].item.kind == token_kinds.identifier):
                stack[-1] = StackItem(tree.IdentifierNode(stack[-1].item), 1)

            # If the top of the stack matches a binary operator, reduce it to
            # an expression node.
            elif (len(stack) >= 3 and isinstance(stack[-1].item, tree.Node) and
                  isinstance(stack[-2].item, Token) and
                  stack[-2].item.kind in binary_operators.keys() and
                  isinstance(stack[-3].item, tree.Node)

                  # Make sure next token is not a higher precedence binary
                  # operator.
                  and not (i < len(self.tokens) and
                           self.tokens[i].kind in binary_operators.keys() and
                           (binary_operators[self.tokens[i].kind] >
                            binary_operators[stack[-2].item.kind]))

                  # Make sure next token is not beginning a function call,
                  # because function call has higher precedence than all binary
                  # operators.
                  and not (i < len(self.tokens) and
                           self.tokens[i].kind == token_kinds.open_paren)

                  # Make sure this and next token are not both assignment
                  # tokens, because assignment tokens are right associative.
                  and not (i < len(self.tokens) and
                           stack[-2].item.kind in assignment_operators and
                           self.tokens[i].kind in assignment_operators)):
                left_expr = stack[-3]
                right_expr = stack[-1]
                operator = stack[-2]

                # Remove these last 3 elements
                del stack[-3:]
                stack.append(
                    StackItem(
                        tree.BinaryOperatorNode(left_expr.item, operator.item,
                                                right_expr.item), left_expr.
                        length + operator.length + right_expr.length))

            # If the top of the stack matches a unary prefix operator, reduce
            # it to an expression node.
            elif (len(stack) >= 2 and isinstance(stack[-1].item, tree.Node) and
                  isinstance(stack[-2].item, Token) and
                  stack[-2].item.kind in unary_prefix_operators

                  # Make sure next token is not beginning a function call,
                  # because function call has higher precedence than
                  # address-of operator.
                  and not (i < len(self.tokens) and
                           self.tokens[i].kind == token_kinds.open_paren)):

                expr = stack[-1]
                op = stack[-2]
                node = unary_prefix_operators[op.item.kind]

                del stack[-2:]
                stack.append(StackItem(node(expr.item, op.item),
                                       1 + expr.length))

            # If the top of the stack matches an identifier followed by a pair
            # of parentheses, reduce it to a function call node.
            elif self.match_function_call(stack):
                func, args = self.match_function_call(stack)

                # Compute number of tokens to delete
                if not args: num_delete = 3
                else: num_delete = 2 + 2 * len(args)

                # Compute size
                size = sum(el.length for el in stack[-num_delete:])

                arg_items = list(map(lambda x: x.item, args))

                del stack[-num_delete:]
                stack.append(
                    StackItem(tree.FunctionCallNode(func.item, arg_items),
                              size))

            # If the top of the stack matches ( expr ), reduce it to a
            # ParenExpr node. This check must be after function call parsing,
            # because otherwise f(5) would get reduced prematurely.
            elif (len(stack) >= 3 and isinstance(stack[-1].item, Token) and
                  stack[-1].item.kind == token_kinds.close_paren and
                  isinstance(stack[-2].item, tree.Node) and
                  isinstance(stack[-3].item, Token) and
                  stack[-3].item.kind == token_kinds.open_paren):
                expr = stack[-2]

                del stack[-3:]
                stack.append(
                    StackItem(tree.ParenExprNode(expr.item), expr.length + 2))

            else:
                # If we're at the end of the token list, or we've reached a
                # token that can never appear in an expression, stop reading.
                # Note we must update this every time the parser is expanded to
                # accept more identifiers.

                # Printing stack here is helpful for debugging.
                # print(stack)

                if i == len(self.tokens):
                    break
                elif (self.tokens[i].kind != token_kinds.number and
                      self.tokens[i].kind != token_kinds.identifier and
                      self.tokens[i].kind != token_kinds.open_paren and
                      self.tokens[i].kind != token_kinds.close_paren and
                      self.tokens[i].kind != token_kinds.comma and
                      self.tokens[i].kind != token_kinds.amp and
                      self.tokens[i].kind not in binary_operators.keys()):
                    break

                stack.append(StackItem(self.tokens[i], 1))
                i += 1

        if stack and isinstance(stack[0].item, tree.Node):
            return (stack[0].item, index + stack[0].length)
        else:
            err = "expected expression"
            raise ParserError(err, index, self.tokens, ParserError.GOT)

    type_tokens = {token_kinds.void_kw: ctypes.void,
                   token_kinds.bool_kw: ctypes.bool_t,
                   token_kinds.char_kw: ctypes.char,
                   token_kinds.short_kw: ctypes.short,
                   token_kinds.int_kw: ctypes.integer,
                   token_kinds.long_kw: ctypes.longint}

    def parse_declaration(self, index):
        """Parse a declaration.

        Ex: int a, b = 5, *c;

        Currently, only simple declarations of a single arithmetic type without
        an intializer are supported. Signed and unsigned declarations also
        supported.

        """
        # Parse a signed/unsigned declaration or lack thereof
        signed = True
        if self._next_token_is(index, token_kinds.signed_kw):
            signed = True
            index += 1
        elif self._next_token_is(index, token_kinds.unsigned_kw):
            signed = False
            index += 1

        # Parse the type name
        index = self._expect_type_name(index)
        ctype = self.type_tokens[self.tokens[index - 1].kind]
        if not signed:
            ctype = ctypes.to_unsigned(ctype)

        # Parse any number of stars to indicate pointer type
        while self._next_token_is(index, token_kinds.star):
            ctype = PointerCType(ctype)
            index += 1

        # Parse the identifier name
        index = self._match_token(index, token_kinds.identifier,
                                  "expected identifier", ParserError.AFTER)
        variable_name = self.tokens[index - 1]

        # Parse an array declaration
        if (self._next_token_is(index, token_kinds.open_sq_brack) and
            self._next_token_is(index + 1, token_kinds.number) and
             self._next_token_is(index + 2, token_kinds.close_sq_brack)):
            ctype = ArrayCType(ctype, int(self.tokens[index + 1].content))
            index += 3

        index = self._expect_semicolon(index)

        return tree.DeclarationNode(variable_name, ctype), index

    def _expect_type_name(self, index):
        """Expect a type name at self.tokens[index].

        If one is found, return index+1. Otherwise, raise an appropriate
        ParserError.

        """
        err = "expected type name"

        type_tokens = list(self.type_tokens.keys())
        for tok in type_tokens[:-1]:
            try:
                return self._match_token(index, tok, err, ParserError.GOT)
            except ParserError as e:
                self._log_error(e)

        return self._match_token(index, type_tokens[-1], err, ParserError.GOT)

    def _expect_semicolon(self, index):
        """Expect a semicolon at self.tokens[index].

        If one is found, return index+1. Otherwise, raise an appropriate
        ParserError.

        """
        return self._match_token(index, token_kinds.semicolon,
                                 "expected semicolon", ParserError.AFTER)

    def _next_token_is(self, index, kind):
        """Return true iff the next token is of the given kind."""
        return len(self.tokens) > index and self.tokens[index].kind == kind

    def _match_token(self, index, kind, message, message_type):
        """Raise ParserError if tokens[index] is not of the expected kind.

        If tokens[index] is of the expected kind, returns index + 1.
        Otherwise, raises a ParserError with the given message and
        message_type.

        """
        if (len(self.tokens) > index and self.tokens[index].kind == kind):
            return index + 1
        else:
            raise ParserError(message, index, self.tokens, message_type)

    def _log_error(self, error):
        """Log the error in the parser to be used for error reporting.

        The value of error.amount_parsed is used to determine the amount
        successfully parsed before encountering the error.

        error (ParserError) - Error encountered.

        """
        if (not self.best_error or
                error.amount_parsed >= self.best_error.amount_parsed):
            self.best_error = error
