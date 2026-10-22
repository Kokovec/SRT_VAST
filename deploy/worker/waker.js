// Front door for the stopped-when-idle EC2 instance.
//
// Runs on every request to the viewer hostname, after Cloudflare Access, so only
// signed-in users get here. Requests go straight through to the tunnel; when the
// origin is down (instance stopped, or still booting) the Worker asks EC2 to start
// the instance and answers with a page that refreshes until the viewer is up.
//
// Bindings: INSTANCE_ID, AWS_REGION (text); AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
// (secrets) for an IAM user that may only Describe/Start this one instance.

// Cloudflare's "origin unreachable" family: 530 is a tunnel with no connector.
const ORIGIN_DOWN = new Set([502, 503, 504, 521, 522, 523, 524, 530]);
const EC2_API_VERSION = "2016-11-15";

export default {
  async fetch(request, env) {
    let response = null;
    try {
      response = await fetch(request);
    } catch {
      response = null;
    }
    if (response && !ORIGIN_DOWN.has(response.status)) return response;

    let state;
    try {
      state = await instanceState(env);
      if (state === "stopped") {
        await ec2(env, { Action: "StartInstances", "InstanceId.1": env.INSTANCE_ID });
        state = "starting";
      }
    } catch (err) {
      state = `unavailable (${err.message})`;
    }

    const headers = { "Cache-Control": "no-store", "Retry-After": "10" };
    if (!(request.headers.get("Accept") || "").includes("text/html")) {
      return new Response(`stream server ${state}\n`, { status: 503, headers });
    }
    return new Response(startingPage(state), {
      status: 503,
      headers: { ...headers, "Content-Type": "text/html; charset=utf-8" },
    });
  },
};

async function instanceState(env) {
  const xml = await ec2(env, { Action: "DescribeInstances", "InstanceId.1": env.INSTANCE_ID });
  const m = xml.match(/<instanceState>\s*<code>\d+<\/code>\s*<name>([a-z-]+)<\/name>/);
  if (!m) throw new Error("instance not found");
  return m[1];
}

async function ec2(env, params) {
  const host = `ec2.${env.AWS_REGION}.amazonaws.com`;
  const query = canonicalQuery({ ...params, Version: EC2_API_VERSION });
  const { authorization, amzDate } = await signV4({
    method: "GET", host, path: "/", query, region: env.AWS_REGION, service: "ec2",
    accessKeyId: env.AWS_ACCESS_KEY_ID, secretAccessKey: env.AWS_SECRET_ACCESS_KEY,
  });
  const res = await fetch(`https://${host}/?${query}`, {
    headers: { Authorization: authorization, "X-Amz-Date": amzDate },
  });
  const body = await res.text();
  if (!res.ok) {
    const code = (body.match(/<Code>([^<]+)<\/Code>/) || [])[1] || res.status;
    throw new Error(`EC2 ${params.Action}: ${code}`);
  }
  return body;
}

// ---------------------------------------------------------------- SigV4

const enc = new TextEncoder();
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const sha256 = async (text) => hex(await crypto.subtle.digest("SHA-256", enc.encode(text)));
async function hmac(key, text) {
  const k = await crypto.subtle.importKey("raw", typeof key === "string" ? enc.encode(key) : key,
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", k, enc.encode(text));
}
const rfc3986 = (s) => encodeURIComponent(s).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());

export function canonicalQuery(params) {
  return Object.entries(params)
    .map(([k, v]) => [rfc3986(k), rfc3986(String(v))])
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
}

/** AWS Signature Version 4 for a request signed on host + x-amz-date only. */
export async function signV4({ method, host, path, query, region, service, accessKeyId, secretAccessKey, amzDate }) {
  amzDate = amzDate || new Date().toISOString().replace(/[:-]|\.\d{3}/g, "");
  const date = amzDate.slice(0, 8);
  const signedHeaders = "host;x-amz-date";
  const canonicalRequest = [method, path, query, `host:${host}\nx-amz-date:${amzDate}\n`, signedHeaders,
    await sha256("")].join("\n");
  const scope = `${date}/${region}/${service}/aws4_request`;
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, await sha256(canonicalRequest)].join("\n");
  let key = await hmac(`AWS4${secretAccessKey}`, date);
  key = await hmac(key, region);
  key = await hmac(key, service);
  key = await hmac(key, "aws4_request");
  const signature = hex(await hmac(key, stringToSign));
  return {
    amzDate,
    signature,
    authorization: `AWS4-HMAC-SHA256 Credential=${accessKeyId}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
  };
}

// ---------------------------------------------------------------- page

function startingPage(state) {
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const note = state === "running"
    ? "The server is up; the stream services are starting."
    : state === "stopping"
      ? "The server is just shutting down after being idle; it will start again in a moment."
      : "The stream server was asleep and is starting.";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10"><title>Starting SRT VAST</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0d1014; color: #d9e1ea;
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding: 16px; box-sizing: border-box; }
  main { max-width: 440px; text-align: center; }
  h1 { font-size: 20px; margin: 0 0 8px; }
  p { color: #7d8a99; margin: 6px 0; }
  .spin { width: 28px; height: 28px; margin: 0 auto 18px; border: 3px solid #262f3a; border-top-color: #2fbf71;
          border-radius: 50%; animation: s 1s linear infinite; }
  @keyframes s { to { transform: rotate(360deg); } }
  code { color: #d9e1ea; }
  a { color: #d9e1ea; }
</style></head>
<body><main>
  <div class="spin" aria-hidden="true"></div>
  <h1>Starting the stream server…</h1>
  <p>${note} This usually takes 1–2 minutes; this page reloads by itself.</p>
  <p>Instance state: <code>${esc(state)}</code></p>
  <p><a href="/cdn-cgi/access/logout">Log out</a></p>
</main></body></html>`;
}
