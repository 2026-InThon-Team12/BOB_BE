// Browser reference adapter. Native Android should supply an equivalent encrypted
// app-private store. No guest token is written to this store or returned to logs.
export class LocalQuestionStore {
  constructor({ guestId, request, storage = globalThis.localStorage, today = seoulDate }) {
    if (!guestId || !storage || typeof request !== 'function') throw new Error('Missing store configuration');
    this.guestId = guestId;
    this.request = request; // authenticated (path, {method, body}) => parsed JSON
    this.storage = storage;
    this.today = today;
    this.pending = new Map();
  }

  key(phase, id) { return `bob:questions:v1:${JSON.stringify([this.guestId, phase, id])}`; }
  read(phase, id) { return JSON.parse(this.storage.getItem(this.key(phase, id)) || '[]'); }
  write(phase, id, records) { this.storage.setItem(this.key(phase, id), JSON.stringify(records)); }

  async question(phase, id) {
    if (!['pre', 'post'].includes(phase)) throw new Error('Invalid phase');
    const day = this.today();
    const existing = this.read(phase, id).find(row => row.question_date === day);
    if (existing) return existing;
    const key = this.key(phase, id) + ':' + day;
    if (this.pending.has(key)) return this.pending.get(key);
    const promise = (async () => {
      const recent_templates = this.read(phase, id).filter(row => row.question_date < day).map(row => ({
        date: row.question_date, template_code: row.template_code,
      }));
      const resource = phase === 'pre' ? 'simulations' : 'purchases';
      const generated = await this.request(`/api/v1/${resource}/${encodeURIComponent(id)}/local-question`, {
        method: 'POST', body: { recent_templates },
      });
      // The server's Seoul date is authoritative; never store yesterday's result as today's card.
      if (generated.question_date !== this.today()) throw new Error('Date changed; reload today');
      const records = this.read(phase, id);
      const wonRace = records.find(row => row.question_date === generated.question_date);
      if (wonRace) return wonRace;
      records.push(generated);
      this.write(phase, id, records); // fail visibly on quota/private-storage errors
      return generated;
    })();
    this.pending.set(key, promise);
    try { return await promise; } finally { this.pending.delete(key); }
  }

  answer(phase, id, answer) {
    const records = this.read(phase, id);
    const current = records.find(row => row.question_date === this.today());
    if (!current) throw new Error('Generate today’s question first');
    validateAnswer(current.answer_spec, answer);
    current.answer = { ...answer };
    this.write(phase, id, records);
    return current; // no network request
  }

  answered(phase, id) {
    return !!this.read(phase, id).find(row => row.question_date === this.today())?.answer;
  }

  async summary(simulationId) {
    const responses = this.read('pre', simulationId).filter(row => row.answer).map(row => ({
      date: row.question_date, template_code: row.template_code, question: row.question, answer: row.answer,
    }));
    const result = await this.request(`/api/v1/simulations/${encodeURIComponent(simulationId)}/result-summary`, {
      method: 'POST', body: { responses },
    });
    this.storage.setItem(this.key('summary', simulationId), JSON.stringify(result));
    return result;
  }
}

export function seoulDate() {
  const parts = new Intl.DateTimeFormat('en', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit' })
    .formatToParts(new Date());
  const value = type => parts.find(part => part.type === type).value;
  return `${value('year')}-${value('month')}-${value('day')}`;
}

function validateAnswer(spec, answer) {
  const entries = Object.entries(answer).filter(([, value]) => value != null);
  if (entries.length !== 1) throw new Error('Provide exactly one answer');
  const [kind, value] = entries[0];
  let valid = false;
  if (kind === 'choice_code') valid = (spec.options || []).some(option => option.code === value);
  if (kind === 'number_value') valid = spec.type === 'INTEGER' && Number.isSafeInteger(value) && value >= spec.minimum && value <= spec.maximum;
  if (kind === 'text_value') valid = spec.type === 'TEXT' && typeof value === 'string' && value.trim().length > 0 && value.length <= spec.max_length;
  if (kind === 'object_value' && spec.type === 'OBJECT' && value && typeof value === 'object') {
    valid = Object.keys(value).length === Object.keys(spec.fields).length && Object.entries(spec.fields).every(([field, schema]) =>
      schema.options.some(option => option.code === value[field]));
  }
  if (!valid) throw new Error('Answer does not match this question');
}
