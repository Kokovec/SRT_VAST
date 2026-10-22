#!/usr/bin/env bash
# Authorize the stack to run again after the monthly budget action fired:
# reverses the executed budget actions (detaches the "deny StartInstances"
# policy from the starter Lambda's role). The next sign-in starts the instance.
#
#   AWS_PROFILE=<admin profile> deploy/budget-resume.sh [budget-name]
#
# If spend is still over the limit this month, the budget may fire again;
# raise budget_limit_usd in deploy/bootstrap and apply to keep running.
set -euo pipefail
BUDGET=${1:-srt-vast-monthly}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

actions=$(aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" \
  --query 'Actions[?Status==`EXECUTION_SUCCESS` || Status==`REVERSE_FAILURE`].[ActionId,ActionType]' --output text)
if [[ -z "$actions" ]]; then
  echo "No executed budget actions on $BUDGET: nothing to resume."
  exit 0
fi
while read -r id type; do
  echo "reversing $type ($id)"
  aws budgets execute-budget-action --account-id "$ACCOUNT" --budget-name "$BUDGET" \
    --action-id "$id" --execution-type REVERSE_BUDGET_ACTION > /dev/null
done <<< "$actions"
echo "Done. The next sign-in starts the instance."
