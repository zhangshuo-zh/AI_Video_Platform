/** Retain an operation key across unknown-result retries; changed input starts a new operation. */
export function submissionIdentity() {
  let signature = null;
  let key = null;
  return {
    key(body) {
      const next = JSON.stringify(body);
      if (next !== signature) {
        signature = next;
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        key = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
      }
      return key;
    },
    clear() { signature = null; key = null; },
  };
}
