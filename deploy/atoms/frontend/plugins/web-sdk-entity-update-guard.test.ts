// Run with Node 22+: node --experimental-strip-types --test plugins/web-sdk-entity-update-guard.test.ts
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { webSdkEntityUpdateGuard } from './web-sdk-entity-update-guard.ts';

const sourceRoot = '/workspace/app/frontend/src';

function check(code: string, file = `${sourceRoot}/components/Fixture.tsx`) {
  const plugin = webSdkEntityUpdateGuard(sourceRoot);
  const transform = plugin.transform as (code: string, id: string) => unknown;
  return transform(code, file);
}

for (const method of [
  'get',
  'create',
  'update',
  'delete',
  'query',
  'queryAll',
]) {
  test(`rejects positional and scalar ${method} arguments`, () => {
    for (const args of [
      'row.id, { done: true }',
      "'123'",
      '`123`',
      '`id-${row.id}`',
      '123',
      '-123',
      '123n',
      'true',
      '[]',
      '() => ({ id: 1 })',
    ]) {
      assert.throws(
        () => check(`client.entities.tasks.${method}(${args});`),
        new RegExp(`entity ${method} requires`)
      );
    }
  });

  test(`accepts object literal, variable and property ${method} arguments`, () => {
    for (const code of [
      `client.entities.tasks.${method}({ id: '123', data: { done: true } });`,
      `const params = { id: '123', data: { done: true } }; client.entities.tasks.${method}(params);`,
      `const holder = { params: { id: '123' } }; client.entities.tasks.${method}(holder.params);`,
      `import { params } from './params'; client.entities.tasks.${method}(params);`,
    ]) {
      assert.equal(check(code), null);
    }
  });
}

test('requires an argument for get/create/update/delete and preserves optional query parameters', () => {
  for (const method of ['get', 'create', 'update', 'delete']) {
    for (const args of ['', 'null', 'void 0']) {
      assert.throws(() => check(`client.entities.tasks.${method}(${args});`));
    }
  }
  for (const method of ['query', 'queryAll']) {
    for (const args of ['', 'undefined', 'null', 'void 0']) {
      assert.equal(check(`client.entities.tasks.${method}(${args});`), null);
    }
  }
});

test('recognizes bracket access and wrapped arguments', () => {
  for (const code of [
    "api.client.entities['tasks']['delete']('123');",
    "client.entities.tasks.get(('123' as string));",
    "client.entities.tasks.get('123' as any);",
    'client.entities.tasks.update(row.id, { done: true });',
  ]) {
    assert.throws(() => check(code), /web-sdk entity/);
  }
  assert.equal(
    check(
      'client.entities.tasks.get(({ id: "123" } satisfies { id: string }));'
    ),
    null
  );
});

test('rejects bare ids and object literals that omit required fields', () => {
  for (const code of [
    'client.entities.tasks.delete(row.id);',
    'client.entities.tasks.get(taskId);',
    'client.entities.tasks.get(item.task_id);',
    'client.entities.tasks.get(item.taskID);',
    'client.entities.tasks.get(row["id"]);',
    'client.entities.tasks.update({ id: row.id, done: true });',
    "client.entities.tasks.create({ title: 'x' });",
    "client.entities.tasks.get({ fields: ['id'] });",
    'client.entities.tasks.delete({});',
    "client.entities.tasks.update({ data: { done: true } });",
  ]) {
    assert.throws(() => check(code), /web-sdk entity/);
  }
});

test('rejects coerced ids, undefined, and non-null or asserted receivers', () => {
  for (const code of [
    'client.entities.tasks.delete(String(row.id));',
    'client.entities.tasks.get(Number(id));',
    'client.entities.tasks.get(parseInt(id));',
    'client.entities.tasks.get(Number.parseInt(id));',
    'client.entities.tasks.update(row.id.toString());',
    'client.entities.tasks.get((String(row.id)));',
    'client.entities.tasks.get(undefined);',
    'client.entities.tasks.update(undefined);',
    'client.entities.tasks.create(undefined);',
    'client!.entities.tasks.delete(row.id);',
    '(client.entities as any).tasks.delete(row.id);',
    'client.entities.tasks!.delete(row.id);',
  ]) {
    assert.throws(() => check(code), /web-sdk entity/);
  }
});

test('does not infer other names and leaves unrelated methods alone', () => {
  for (const code of [
    'client.entities.tasks.get(params);',
    'client.entities.tasks.get(holder.params);',
    'client.entities.tasks.get(row);',
    'client.entities.tasks.get(row.name);',
    'client.entities.tasks.get(row[key]);',
    'client.entities.tasks.get(buildParams());',
    'client.entities.tasks.query(String(status));',
    'const api = client.entities.tasks; api.delete(row.id);',
    'const { tasks } = client.entities; tasks.delete(row.id);',
    'client.entities.tasks.update({ id, data });',
    'client.entities.tasks.update({ ...payload });',
    'client.entities.tasks.update({ id: row.id, ...rest });',
    "client.entities.tasks.create({ data: { title: 'x' } });",
    "client.entities.tasks.query({ query: { status: 'active' } });",
    'cache.update(row.id, { done: true });',
    'store.entities.tasks.update(row.id, { done: true });',
    'client.entities.tasks.custom(row.id, { done: true });',
    '// client.entities.tasks.update(row.id, { done: true })',
    'const example = "client.entities.tasks.delete(row.id)";',
  ]) {
    assert.equal(check(code), null);
  }
});

test('checks frontend source files with query suffixes and reports the call location', () => {
  for (const extension of ['ts', 'tsx', 'js', 'jsx']) {
    assert.throws(
      () =>
        check(
          "\nclient.entities.tasks.get('123');",
          `${sourceRoot}/Fixture.${extension}?v=1`
        ),
      new RegExp(`Fixture\\.${extension}:2:1: web-sdk entity get`)
    );
  }
  for (const file of [
    '/workspace/app/backend/src/Fixture.ts',
    '/workspace/app/frontend/src-other/Fixture.ts',
    `${sourceRoot}/styles.css`,
  ]) {
    assert.equal(
      check('client.entities.tasks.update(row.id, { done: true });', file),
      null
    );
  }
});
