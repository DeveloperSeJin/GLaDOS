---
name: experiment
description: "Do an experiment or a data analysis on this computer: write a script, run it, check the results, and write them up."
tools: ["fs.write_new", "fs.read", "fs.edit", "fs.search", "shell.run"]
stages:
  - name: hypothesis
    goal: "State the question, a testable hypothesis, and the result that would support or reject it."
    gate: "A testable hypothesis and the result that would reject it are stated."
    tools: ["fs.read", "fs.search"]
    approve: true
  - name: design
    goal: "Fix the variables, the measure, the statistical test and what it assumes, the sample size and repeats, the data, and a random seed."
    gate: "The design names the test, why it fits the data and the hypothesis, the sample size, and a fixed seed."
    tools: ["fs.read", "fs.search"]
  - name: implement
    goal: "Write the experiment as a new script in the experiments folder ({experiments}) with fs.write_new: the settings and the seed at the top, the results printed at the end."
    gate: "A tool result shows the script for this design was saved, with its path."
    tools: ["fs.write_new", "fs.edit", "fs.read"]
  - name: test
    goal: "Check the method before trusting it: run it on cases with a known answer (for example data that surely follows the hypothesis and data that surely does not), or reproduce an earlier result."
    gate: "A tool result shows the method gives the expected answer on the known cases."
    tools: ["shell.run", "fs.write_new", "fs.edit", "fs.read"]
  - name: run
    goal: "Run the script with shell.run."
    gate: "A tool result shows the script's full output without errors."
    tools: ["shell.run", "fs.read"]
  - name: analyze
    goal: "Read the output: the test result, effect sizes, distributions, and failure cases."
    gate: "Every number to report appears in a tool result, and none is impossible for the method (a p-value of exactly 1 or 0, a statistic of exactly 0, every value the same)."
    tools: ["shell.run", "fs.read"]
  - name: conclude
    goal: "Judge the hypothesis (supported, rejected, or not tested), say what the test does not show, and suggest the next hypothesis. Save a report only if asked."
    gate: "The verdict follows from the analyzed numbers."
    tools: ["fs.write_new", "fs.read"]
---
You are running an experiment on this computer, one stage at a time.
- When asked to summarize, tabulate, or write up earlier work, use the results already in this conversation; do not run it again. If it must run again, say why, and use the same script and seed so the numbers stay the same.
- When you report a statistical test, name the test and what it assumes, and say what the result shows and what it does not show.
