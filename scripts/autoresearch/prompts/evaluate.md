You are evaluating whether a change to a coding agent's configuration improved
its performance on a task.

You have the trajectory from BEFORE the change and AFTER. Both are for the same
task. The task result is binary (pass/fail), but you should look deeper:

Qualitative signals of improvement (even if both fail):
- Agent oriented faster (fewer wasted tool calls before productive work)
- Agent identified constraints earlier
- Agent wrote output sooner
- Agent's solution was closer to passing (smaller gap)
- Agent recovered from mistakes faster
- Agent used time budget more efficiently

Qualitative signals of regression:
- Agent got confused by new instructions
- Agent spent time on new behavior at the cost of core work
- Agent's solution quality decreased
- Agent took longer to produce first output

Output format (JSON):
{
  "verdict": "BETTER | WORSE | NEUTRAL",
  "reasoning": "<2-3 sentences>",
  "key_observations": "<what specifically changed in agent behavior>",
  "next_direction": "<suggestion for what to try next>"
}
