import ts from 'typescript';
import { normalizePath, type Plugin } from 'vite';

const entityMethods = new Set([
  'get',
  'create',
  'update',
  'delete',
  'query',
  'queryAll',
]);
const optionalParamsMethods = new Set(['query', 'queryAll']);
const idPathMethods = new Set(['get', 'update', 'delete']);
// Names that are almost always a scalar id. `params` and `holder.params` do not match.
const bareIdName = /^(?:id|.+Id|.+_id|.+ID)$/;
const scalarCoercions = new Set(['String', 'Number', 'parseInt']);

function unwrap(expression: ts.Expression): ts.Expression {
  if (
    ts.isParenthesizedExpression(expression) ||
    ts.isAsExpression(expression) ||
    ts.isTypeAssertionExpression(expression) ||
    ts.isSatisfiesExpression(expression) ||
    ts.isNonNullExpression(expression)
  ) {
    return unwrap(expression.expression);
  }
  return expression;
}

function propertyNameText(name: ts.PropertyName): string | undefined {
  if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name))
    return name.text;
  if (!ts.isComputedPropertyName(name)) return undefined;
  const expression = unwrap(name.expression);
  return ts.isStringLiteral(expression) ? expression.text : undefined;
}

function isBareIdArgument(argument: ts.Expression): boolean {
  const expression = unwrap(argument);
  const name = ts.isIdentifier(expression)
    ? expression.text
    : ts.isPropertyAccessExpression(expression)
      ? expression.name.text
      : ts.isElementAccessExpression(expression) &&
          expression.argumentExpression &&
          ts.isStringLiteral(expression.argumentExpression)
        ? expression.argumentExpression.text
        : undefined;
  return name !== undefined && bareIdName.test(name);
}

function isScalarIdCoercion(argument: ts.Expression): boolean {
  const expression = unwrap(argument);
  if (!ts.isCallExpression(expression)) return false;
  const callee = unwrap(expression.expression);
  if (ts.isIdentifier(callee)) return scalarCoercions.has(callee.text);
  return (
    ts.isPropertyAccessExpression(callee) &&
    (callee.name.text === 'toString' || callee.name.text === 'parseInt')
  );
}

function missingLiteralFields(
  method: string,
  argument: ts.Expression
): string | undefined {
  const expression = unwrap(argument);
  if (!ts.isObjectLiteralExpression(expression)) return undefined;
  const names = new Set<string>();
  for (const property of expression.properties) {
    if (ts.isSpreadAssignment(property)) return undefined;
    if (!property.name) continue;
    const name = propertyNameText(property.name);
    if (name === undefined) return undefined;
    names.add(name);
  }
  if (method === 'create') return names.has('data') ? undefined : 'data';
  if (method === 'update') {
    const missing = ['id', 'data'].filter(name => !names.has(name));
    return missing.length ? missing.join(' and ') : undefined;
  }
  if (idPathMethods.has(method)) return names.has('id') ? undefined : 'id';
  return undefined;
}

function usesClientEntities(expression: ts.Expression): boolean {
  const current = unwrap(expression);
  if (ts.isPropertyAccessExpression(current)) {
    if (current.name.text === 'entities') {
      const owner = unwrap(current.expression);
      return (
        (ts.isIdentifier(owner) && owner.text === 'client') ||
        (ts.isPropertyAccessExpression(owner) && owner.name.text === 'client')
      );
    }
    return usesClientEntities(current.expression);
  }
  if (ts.isElementAccessExpression(current)) {
    return usesClientEntities(current.expression);
  }
  return false;
}

export function webSdkEntityUpdateGuard(sourceRoot: string): Plugin {
  const prefix = `${normalizePath(sourceRoot).replace(/\/$/, '')}/`;

  return {
    name: 'web-sdk-entity-update-guard',
    enforce: 'pre',
    transform(code, id) {
      const file = normalizePath(id.split('?', 1)[0]);
      if (
        !file.startsWith(prefix) ||
        !/\.[jt]sx?$/.test(file) ||
        !code.includes('entities') ||
        ![...entityMethods].some(method => code.includes(method))
      ) {
        return null;
      }

      const source = ts.createSourceFile(
        file,
        code,
        ts.ScriptTarget.Latest,
        true
      );
      const isNonObjectArgument = (
        argument: ts.Expression,
        optional: boolean
      ): boolean => {
        const expression = unwrap(argument);
        return (
          ts.isStringLiteralLike(expression) ||
          ts.isTemplateExpression(expression) ||
          ts.isNumericLiteral(expression) ||
          ts.isBigIntLiteral(expression) ||
          ts.isPrefixUnaryExpression(expression) ||
          ts.isTypeOfExpression(expression) ||
          ts.isDeleteExpression(expression) ||
          expression.kind === ts.SyntaxKind.TrueKeyword ||
          expression.kind === ts.SyntaxKind.FalseKeyword ||
          ts.isArrayLiteralExpression(expression) ||
          ts.isArrowFunction(expression) ||
          ts.isFunctionExpression(expression) ||
          (!optional &&
            (expression.kind === ts.SyntaxKind.NullKeyword ||
              ts.isVoidExpression(expression) ||
              (ts.isIdentifier(expression) && expression.text === 'undefined')))
        );
      };
      const visit = (node: ts.Node): void => {
        if (
          ts.isCallExpression(node) &&
          (ts.isPropertyAccessExpression(node.expression) ||
            ts.isElementAccessExpression(node.expression)) &&
          usesClientEntities(node.expression.expression)
        ) {
          const method = ts.isPropertyAccessExpression(node.expression)
            ? node.expression.name.text
            : ts.isStringLiteral(node.expression.argumentExpression)
              ? node.expression.argumentExpression.text
              : '';
          const optional = optionalParamsMethods.has(method);
          const argument = node.arguments[0];
          const bareId =
            node.arguments.length === 1 &&
            idPathMethods.has(method) &&
            (isBareIdArgument(argument) || isScalarIdCoercion(argument));
          const missing =
            node.arguments.length === 1
              ? missingLiteralFields(method, argument)
              : undefined;
          if (
            entityMethods.has(method) &&
            (node.arguments.length > 1 ||
              (!optional && node.arguments.length === 0) ||
              (node.arguments.length === 1 &&
                isNonObjectArgument(argument, optional)) ||
              bareId ||
              missing !== undefined)
          ) {
            const { line, character } = source.getLineAndCharacterOfPosition(
              node.getStart(source)
            );
            const example =
              method === 'update'
                ? '{ id, data }'
                : method === 'create'
                  ? '{ data }'
                  : optional
                    ? '{ query, sort, limit, skip, fields }'
                    : '{ id }';
            const reason = missing
              ? `the object literal must include ${missing}`
              : bareId
                ? 'a bare or coerced id is read as the parameter object and produces an /undefined request'
                : 'positional or scalar arguments do not match the SDK contract';
            throw new Error(
              `${file}:${line + 1}:${character + 1}: web-sdk entity ${method} requires ${method}(${example})${optional ? ' or no argument' : ''}; ${reason}.`
            );
          }
        }
        ts.forEachChild(node, visit);
      };
      visit(source);
      return null;
    },
  };
}
