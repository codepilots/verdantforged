# EC2 user-data and redeploy pitfalls

Session note from 2026-06-29:
- EC2 `RunInstances` rejects user-data above 16,384 bytes. A bloated `worker/user-data.sh` caused `InvalidParameterValue: User data is limited to 16384 bytes`.
- Fix: keep user-data as a tiny bootstrap stub; move large helpers, mock servers, and install payloads onto EFS or S3 and invoke them from there.
- In this repo, the worker bootstrap shrank below the limit after removing an inline mock LLM heredoc.
- Redeploys of an existing CloudFormation stack can return `UPDATE_COMPLETE`, not `CREATE_COMPLETE`. Any wait loop that only accepts `CREATE_COMPLETE` will hang forever on subsequent deploys.
- Smoke tests can briefly 502 while Caddy gets a cert and broker-daemon comes up; verify the service state and logs before assuming the deploy failed.
