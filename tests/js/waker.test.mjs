// node --test tests/js/  — SigV4 against the AWS test suite "get-vanilla" vector,
// and the Worker's front-door behaviour with fetch mocked.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { canonicalQuery, signV4 } from "../../deploy/worker/waker.js";

test("SigV4 matches AWS get-vanilla test vector", async () => {
  const { signature } = await signV4({
    method: "GET", host: "example.amazonaws.com", path: "/", query: "", region: "us-east-1", service: "service",
    accessKeyId: "AKIDEXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", amzDate: "20150830T123600Z",
  });
  assert.equal(signature, "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31");
});

test("canonical query is sorted and RFC 3986 encoded", () => {
  assert.equal(canonicalQuery({ Version: "2016-11-15", Action: "Describe Instances", "InstanceId.1": "i-1" }),
    "Action=Describe%20Instances&InstanceId.1=i-1&Version=2016-11-15");
});

const env = { INSTANCE_ID: "i-0abc", AWS_REGION: "us-east-1", AWS_ACCESS_KEY_ID: "AKID", AWS_SECRET_ACCESS_KEY: "secret" };
const describe = (state) => `<DescribeInstancesResponse><instanceState><code>80</code><name>${state}</name></instanceState></DescribeInstancesResponse>`;

function mockFetch(origin, ec2State) {
  const calls = [];
  globalThis.fetch = async (input) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push(url);
    if (url.startsWith("https://ec2.")) {
      const action = new URL(url).searchParams.get("Action");
      return new Response(action === "DescribeInstances" ? describe(ec2State) : "<ok/>", { status: 200 });
    }
    return origin;
  };
  return calls;
}

test("passes through when the origin is up", async () => {
  const calls = mockFetch(new Response("viewer", { status: 200 }), "running");
  const res = await worker.fetch(new Request("https://srt.example.com/"), env);
  assert.equal(await res.text(), "viewer");
  assert.equal(calls.length, 1);
});

test("starts a stopped instance and shows the starting page", async () => {
  const calls = mockFetch(new Response("", { status: 530 }), "stopped");
  const res = await worker.fetch(new Request("https://srt.example.com/", { headers: { Accept: "text/html" } }), env);
  assert.equal(res.status, 503);
  assert.match(await res.text(), /Starting the stream server/);
  assert.ok(calls.some((u) => u.includes("Action=StartInstances") && u.includes("InstanceId.1=i-0abc")));
});

test("does not start an instance that is already starting", async () => {
  const calls = mockFetch(new Response("", { status: 530 }), "pending");
  const res = await worker.fetch(new Request("https://srt.example.com/live.ts"), env);
  assert.equal(res.status, 503);
  assert.equal(await res.text(), "stream server pending\n");
  assert.ok(!calls.some((u) => u.includes("StartInstances")));
});
