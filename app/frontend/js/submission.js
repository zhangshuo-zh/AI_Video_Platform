export function newSubmissionKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Retain an operation key across unknown-result retries; changed input starts a new operation. */
export function submissionIdentity() {
  let signature = null;
  let key = null;
  return {
    key(body) {
      const next = JSON.stringify(body);
      if (next !== signature) {
        signature = next;
        key = newSubmissionKey();
      }
      return key;
    },
    clear() { signature = null; key = null; },
  };
}
