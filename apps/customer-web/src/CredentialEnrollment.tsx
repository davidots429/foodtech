import { useRef, useState, type FormEvent } from "react";

type Provider = "openai" | "google" | "anthropic";
type TransferKey = {
  key_version: string;
  algorithm: "RSA-OAEP-SHA-256";
  public_key_spki_base64: string;
  expires_at: string;
};

function bytesFromBase64(value: string): Uint8Array {
  return Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
}

async function encrypt(secret: Uint8Array, descriptor: TransferKey): Promise<string> {
  if (descriptor.algorithm !== "RSA-OAEP-SHA-256" || Date.parse(descriptor.expires_at) <= Date.now()) {
    throw new Error("전송 공개키가 만료되었습니다. 다시 시도해 주세요.");
  }
  const publicBytes = bytesFromBase64(descriptor.public_key_spki_base64);
  const publicBuffer = new ArrayBuffer(publicBytes.length);
  new Uint8Array(publicBuffer).set(publicBytes);
  const key = await crypto.subtle.importKey("spki", publicBuffer,
    { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
  const secretBuffer = new ArrayBuffer(secret.length);
  new Uint8Array(secretBuffer).set(secret);
  let ciphertext: ArrayBuffer;
  try {
    ciphertext = await crypto.subtle.encrypt({ name: "RSA-OAEP" }, key, secretBuffer);
  } finally {
    new Uint8Array(secretBuffer).fill(0);
  }
  return btoa(String.fromCharCode(...new Uint8Array(ciphertext)));
}

export function CredentialEnrollment({ csrf, onRegistered }: {
  csrf: string; onRegistered?: (credentialId: string) => void;
}) {
  const [provider, setProvider] = useState<Provider>("openai");
  const [secretValue, setSecretValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const cachedKey = useRef<TransferKey | null>(null);
  const active = useRef(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (active.current) return;
    active.current = true;
    setBusy(true);
    setMessage("");
    setError("");
    const secret = new TextEncoder().encode(secretValue);
    setSecretValue("");
    try {
      if (!window.isSecureContext || !crypto.subtle) throw new Error("안전한 HTTPS 환경에서만 등록할 수 있습니다.");
      if (secret.length < 1 || secret.length > 400) throw new Error("API 키는 UTF-8 기준 1~400바이트여야 합니다.");
      let descriptor = cachedKey.current;
      if (!descriptor || Date.parse(descriptor.expires_at) <= Date.now() + 30000) {
        const keyResponse = await fetch("/api/customer/credentials/transfer-key", {
          credentials: "same-origin", cache: "no-store",
        });
        if (!keyResponse.ok) throw new Error("전송 공개키를 받지 못했습니다.");
        descriptor = await keyResponse.json() as TransferKey;
        cachedKey.current = descriptor;
      }
      const ciphertext_base64 = await encrypt(secret, descriptor);
      const response = await fetch("/api/customer/credentials", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ provider, key_version: descriptor.key_version, ciphertext_base64 }),
      });
      if (!response.ok) throw new Error("등록을 완료하지 못했습니다. 새 키를 입력해 다시 시도해 주세요.");
      const result: { credential_id: string } = await response.json();
      onRegistered?.(result.credential_id);
      setMessage(`등록되었습니다. 자격증명 ID: ${result.credential_id}`);
    } catch (failure) {
      cachedKey.current = null;
      setError(failure instanceof Error ? failure.message : "등록을 완료하지 못했습니다.");
    } finally {
      secret.fill(0);
      active.current = false;
      setBusy(false);
    }
  }

  return <section aria-labelledby="credential-title">
    <h2 id="credential-title">외부 AI 자격증명 등록</h2>
    <p>공급자 API 키를 등록할 수 있습니다. 등록만으로 외부 AI 호출이나 공급자 API 과금이 실행되지는 않습니다.</p>
    <p className="small">키는 이 브라우저에서 RSA-OAEP-SHA-256으로 암호화한 뒤 전송합니다. 저장 후 키 값은 다시 표시되지 않습니다.</p>
    <form onSubmit={submit} className="credential-form" autoComplete="off">
      <label>공급자
        <select value={provider} onChange={(event) => setProvider(event.target.value as Provider)} disabled={busy}>
          <option value="openai">OpenAI</option><option value="google">Google</option><option value="anthropic">Anthropic</option>
        </select>
      </label>
      <label>API 키
        <input type="password" value={secretValue} onChange={(event) => setSecretValue(event.target.value)}
          disabled={busy} autoComplete="new-password" spellCheck={false} maxLength={400} required />
      </label>
      <button type="submit" disabled={busy}>{busy ? "암호화·등록 중" : "자격증명 등록"}</button>
    </form>
    {message && <p role="status" className="success">{message}</p>}
    {error && <p role="alert" className="error">{error}</p>}
  </section>;
}
