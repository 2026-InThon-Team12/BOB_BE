import test from 'node:test';
import assert from 'node:assert/strict';
import { LocalQuestionStore } from './local-question-store.mjs';

test('reuses generated question, stores answers offline, sends transcript only for summary', async () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) };
  const calls = [];
  let day = '2026-11-01';
  const request = async (path, options) => {
    calls.push({ path, ...options });
    if (path.endsWith('result-summary')) return { ai_summary: { summary: '요약' } };
    return { id: 'id', question_date: day, template_code: 'EXPECTED_USE_DAYS', question: '30일 중 며칠?',
      answer_spec: { type: 'INTEGER', minimum: 0, maximum: 30 }, answer: null };
  };
  const store = new LocalQuestionStore({ guestId: 'a', storage, request, today: () => day });
  await Promise.all([store.question('pre', 's'), store.question('pre', 's')]);
  assert.equal(calls.length, 1);
  assert.throws(() => store.answer('pre', 's', { number_value: 31 }));
  store.answer('pre', 's', { number_value: 12 });
  assert.equal(calls.length, 1);
  assert.equal(store.answered('pre', 's'), true);
  const restarted = new LocalQuestionStore({ guestId: 'a', storage, request, today: () => day });
  assert.equal((await restarted.question('pre', 's')).answer.number_value, 12);
  await restarted.summary('s');
  assert.equal(calls.at(-1).body.responses[0].answer.number_value, 12);
  day = '2026-11-02';
  assert.equal(store.answered('pre', 's'), false);
  assert.throws(() => store.answer('pre', 's', { number_value: 5 }));
  await store.question('pre', 's');
  assert.deepEqual(calls.at(-1).body, { recent_templates: [{ date: '2026-11-01', template_code: 'EXPECTED_USE_DAYS' }] });
  const other = new LocalQuestionStore({ guestId: 'b', storage, request, today: () => day });
  assert.equal(other.read('pre', 's').length, 0);
});

test('storage errors are visible instead of silently discarding an answer', () => {
  const store = new LocalQuestionStore({ guestId: 'a', request: () => {}, today: () => '2026-11-01', storage: {
    getItem: () => JSON.stringify([{ question_date: '2026-11-01', answer_spec: { type: 'CHOICE', options: [{ code: 'YES' }] } }]),
    setItem: () => { throw new Error('quota'); },
  } });
  assert.throws(() => store.answer('pre', 's', { choice_code: 'YES' }), /quota/);
});
