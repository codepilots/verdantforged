/**
 * /api/agent-init — stretch goal "Send to Hermes" endpoint
 *
 * Receives a POST from the TryInAgent "Send to Hermes" button and creates a
 * kanban task that a Hermes worker can pick up to install the VerdantForged skill.
 *
 * NOT YET WIRED — this endpoint is scaffolded but disabled. Wiring it requires:
 *  1. Auth handshake with the user's connected Hermes agent (OAuth, shared secret, or Nostr challenge)
 *  2. Webhook back to Hermes to receive the install task
 *  3. A kanban card template for skill-install tasks
 *
 * For the hackathon demo, the "B. Copy-paste prompt" path covers 95% of users.
 */
import type { APIRoute } from 'astro';

interface InitRequest {
  agent_id: string;
  skill_url: string;
  callback_url?: string;
}

export const POST: APIRoute = async ({ request }) => {
  let body: InitRequest;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: 'invalid_json' }), {
      status: 400,
      headers: { 'content-type': 'application/json' },
    });
  }

  if (!body.agent_id || !body.skill_url) {
    return new Response(JSON.stringify({ error: 'missing_fields' }), {
      status: 400,
      headers: { 'content-type': 'application/json' },
    });
  }

  // TODO: auth handshake, then create a kanban task or queue a webhook
  // For now, return a 501 indicating the endpoint is scaffolded but not wired
  return new Response(
    JSON.stringify({
      error: 'not_implemented',
      message: 'The /api/agent-init endpoint is scaffolded but not yet wired. Use the "Copy prompt into Hermes" option for now.',
      workaround: {
        copy_prompt_url: '/#try',
        direct_download_url: '/AGENT.md',
      },
    }),
    {
      status: 501,
      headers: { 'content-type': 'application/json' },
    },
  );
};

export const GET: APIRoute = async () => {
  return new Response(
    JSON.stringify({
      endpoint: '/api/agent-init',
      method: 'POST',
      status: 'scaffolded',
      description: 'Stretch goal — not yet wired. See PROPOSAL.md §meta-pitch.',
    }),
    {
      status: 200,
      headers: { 'content-type': 'application/json' },
    },
  );
};
