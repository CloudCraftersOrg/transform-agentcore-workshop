# Keynote script: "Opening the black box: what AWS Transform's agents really do"

**Format:** 60 minutes. A keynote opening, a live demo in the middle, a keynote close, then Q&A.
**What the audience should leave with:** AWS Transform isn't magic. It's a disciplined loop of agents, tools, curated
knowledge and human decisions. Once you understand that loop, you know how to trust it, how to check it, and why it
makes sense to use it instead of building your own.

How to read this script:
- **Plain text** is what you say. It's written to be spoken: short sentences, pauses built in.
- **[SLIDE]**, **[CLICK]**, **[PAUSE]** and **[TERMINAL]** are stage directions.
- **▶ Plan B** is the fallback if something breaks (details in [WORKSHOP.md §8](../WORKSHOP.md#8-plan-b-when-something-breaks-on-stage)).
- Numbers come from the rehearsal run on 2026-10-20. Re-check them after your own rehearsal (cheat sheet at the end).

| Time | Part | Goal |
|---|---|---|
| 00:00–03:00 | Cold open | Get attention with a familiar pain |
| 03:00–15:00 | Part 1: Why we built this | Problem, what AWS Transform is, why we opened the box |
| 15:00–40:00 | Part 2: Live demo | Six agents, one legacy app, about 2 minutes of machine time |
| 40:00–52:00 | Part 3: What it means | Five lessons, and why AWS Transform matters |
| 52:00–55:00 | Call to action | What to do on Monday |
| 55:00–60:00 | Q&A | Prepared answers at the end |

---

## Cold open (00:00–03:00)

**[SLIDE 1: black screen, one line: "Nobody wants to touch the server."]**

Raise your hand if your company has a server that nobody wants to touch.

**[PAUSE, and let the hands go up]**

Keep it up if that server is "temporary".

**[PAUSE]**

It was deployed in 2017. By hand. By someone who left in 2019. It runs a business-critical app.
It has no staging environment, the password is in the source code, and every night a cron job writes a report to a disk that is slowly filling up.

Everybody in this room knows that server. Today we're going to migrate it.

Not with slides. With six AI agents, live, in about twenty minutes, and you'll see every decision they make along the way.

**[SLIDE 2: title, "Opening the black box: what AWS Transform's agents really do"]**

---

## Part 1: Why we built this workshop (03:00–15:00)

### The real problem isn't technology (03:00–06:00)

**[SLIDE 3: "Migrations don't fail because of code. They fail because of knowledge."]**

We've been migrating to the cloud for over fifteen years. We have containers, Kubernetes, Terraform, managed databases. The target is not the problem.

The problem is the *journey*. Before anyone writes a line of Terraform, someone has to answer questions like:
What does this app actually do? What does it depend on? Where are the secrets? What breaks if we move it?
What did the business really ask for? And how much will the new world cost?

That's archaeology, analysis, translation and validation, repeated for every application in the portfolio.
It's slow, it's expensive, and it's exactly the kind of work senior engineers don't want to do a hundred times.

**[SLIDE 4: "Migration debt grows faster than migration teams."]**

So the backlog grows. The servers that nobody wants to touch stay untouched, and every year they get more expensive and riskier.

### Enter AWS Transform (06:00–09:00)

**[SLIDE 5: AWS Transform, agentic AI for migration and modernization]**

AWS Transform is AWS's answer to that backlog: specialized AI agents for migration and modernization work.

- **Assessment**: from your inventory data to a business case and a total cost of ownership.
- **Migration** of VMware and other on-premises estates: discovery, grouping applications by dependency, wave planning,
  converting networks into infrastructure as code (including Terraform), rehosting, and containerization towards ECS or EKS.
- **Windows and .NET**: porting .NET Framework to modern .NET, and SQL Server to Aurora PostgreSQL.
- **Mainframe**: understanding COBOL estates, extracting business rules, and reimagining them as cloud-native code.
- **AWS Transform custom**: language and framework upgrades such as Java, Python, Node, SDK versions and Graviton, plus your *own*
  transformation definitions, which improve from your feedback.

And all along the way, people approve the steps. The agents do the work; humans make the decisions.

**[PAUSE]**

Here's what I've seen in many rooms like this one. Teams look at a tool like this and react in one of two ways.

**[SLIDE 6: two faces. "It's magic!" / "I don't trust it."]**

Some people over-trust it: "The AI will migrate everything, let's go."
Others under-trust it: "It's a black box. I'm not putting my production estate in a black box."

Both reactions come from the same place: *not knowing what happens inside*.

### Why we opened the box (09:00–15:00)

**[SLIDE 7: "So we opened the box."]**

That's why we designed this workshop. We didn't want to show you a product tour. We wanted to show you *the mechanism*.

So we rebuilt the core loop of a migration agent ourselves, at a low level, with the same building blocks you can use
today: **Amazon Bedrock AgentCore** to run the agents, **Strands Agents** as the framework, and Bedrock models
(Amazon Nova and DeepSeek).

Let me be very clear: **what you'll see in the demo is not AWS Transform.** It's a transparent re-creation of the
kind of work its agents do, built so that you can see every tool call, every file, every decision.
When you understand the mechanism, you understand the product.

**[SLIDE 8: the pipeline, Discovery → Requirements → Modernization → Terraform → K8s Validation → FinOps]**

Six agents, one per step of a real migration:

1. **Discovery** reads the code and the infrastructure, and finds the problems.
2. **Requirements** turns the business's wishes into a precise specification.
3. **Modernization** turns the monolith into a containerized microservice.
4. **Terraform** writes the target infrastructure as code.
5. **Kubernetes validation** checks everything against policy and actually deploys it.
6. **FinOps** tells you what it will cost, and whether that's what you agreed to pay.

After every agent, the pipeline stops. A human approves, or asks for changes, in plain language.

**[SLIDE 9: "One thing we learned building this"]**

One more thing before the demo, because it changed how we built everything.

When we started, we did the obvious thing: we asked the models to write the Kubernetes infrastructure *from scratch*.
We benchmarked them, with real tool calls and real `terraform validate`.

**[PAUSE]**

Every single model failed. Not because they're bad models. Because the Terraform modules had changed since they were trained.
They wrote confident, beautiful, *outdated* code. We fed them the errors, again and again, fourteen rounds, and they still didn't get there.

Then we changed one thing. We gave them a **validated blueprint**, a known-good starting point, and asked them to *adapt* it.
They passed. First try.

**[SLIDE 10: "Grounding beats generation."]**

Remember that sentence. It's the most important idea of this talk, and we'll come back to it at the end.

Let's migrate a server.

---

## Part 2: Live demo (15:00–40:00)

> **Before you start:** UI open at http://localhost:8501, mode **Local**, toggles **Live AWS discovery ON**,
> **Live deploy to kind ON**, **Simulate manual drift ON**. Then click **New run**. Terminal ready, with a large font.
> **▶ Plan B for the whole demo:** **…** menu → **Replay a recorded run (offline)** (between stages; mid-stage, use a fresh tab). It's the same UI, and the audience won't notice.

### 15:00 · The patient (2 min)

**[CLICK: browser tab with the legacy app URL]**

Here's our patient. Inventory & Orders. Flask, SQLite, one EC2 instance. Built in 2017.

**[TERMINAL]**
```bash
curl -s "$URL/api/items?search=%25'%20OR%20'1'='1" | head -c 300
```

And here's its secret: this search box returns the *entire* table to anyone who knows a little SQL.
Hard-coded secrets, the Flask development server running as root, reports written to local disk, and a t3.large running at about 4% CPU.

It works. It makes money. And nobody wants to touch it. Let's hand it to the agents.

### 17:00 · Agent 1: Discovery & Assessment (3 min)

**[CLICK: Start migration]**

Watch the timeline on the left. Notice what the agent does *first*: it doesn't "think", it **uses tools**.
It scans the code, parses the old Terraform, and reads the real instance and its CloudWatch metrics.

**[Wait for the result card: about 10 seconds]**

About ten seconds. Seventeen findings. The SQL injection is flagged as **critical**, with the exact function where it lives.
The secrets, the local state and the single point of failure are all there. Readiness score, and a recommended strategy: **replatform**.

Here's the mechanism: **deterministic tools find the evidence; the model weighs it and explains it.**
That's why it's fast, and why you can audit every finding.

> **Real AWS Transform:** this is the discovery and assessment step, at portfolio scale: hundreds of servers,
> dependency mapping, application grouping and the business case. The benefit is weeks of archaeology turned into
> an evidence-backed starting point.

**[CLICK: ✅ Approve & continue]**

### 20:00 · Agent 2: Requirements (4 min)

Now the human side of a migration: what does the business actually want?

**[CLICK: drag in `migration-requirements.md`, or "Use sample requirements"]**

This is a normal requirements document, the kind your business owner writes. Budget of $450 a month. A 99.9% SLO. Graviton. PostgreSQL.
And, like every real document, a few vague points: "maybe 3x more traffic next year".

**[Wait: about 15 seconds]**

Twenty-four requirements, each one with a priority and acceptance criteria, and every one that addresses a problem is **traced** back to what discovery found.
The security finding becomes requirement SEC5. Nothing gets lost between the business and the engineers.

And look at this: **open questions**. The agent didn't invent an answer for "3x traffic". It wrote down an assumption and the impact, and asked us.

**[CLICK: ✏️ Request changes]**, then type:
*"Assume dev/test is out of scope for now; size for today's traffic but keep headroom for 3x growth."*

**[Wait: about 8 seconds, then point at the open questions]**

Look at the open questions. Two of them are now **resolved**: growth and dev/test, with my decision in my words.
The third, the cutover date, is still open, because I didn't answer it.

That's the human in the loop. I don't edit JSON. I talk to the agent, and it changes only what I asked.

> **Real AWS Transform:** every job has the same pattern. Agents propose, humans review and approve each step.
> The benefit is speed without giving up control.

**[CLICK: ✅ Approve & continue]**

### 24:00 · Agent 3: Code modernization (5 min)

This is the longest step, about a minute, so let me tell you what's happening while it runs.

**[CLICK: watch the timeline]**

Remember: grounding beats generation. The agent does **not** write a microservice from a blank page.
Deterministic tools first render a **tested platform pattern**: configuration from the environment, health and readiness endpoints, JSON logs,
a production web server, a non-root, read-only container, and Kubernetes manifests.

The model only does the part that needs intelligence: **porting the business logic** of the old routes, and writing tests for it.
Then it runs those tests. If they fail, it fixes them.

**[Result: about 60 seconds]**

Tests passing. The API contract is preserved, so the warehouse scanners keep working. The SQL injection is gone, because queries are parameterized.
Overselling is now a transaction. Secrets come from the environment.

**[CLICK: open `microservice/app/api.py` next to the legacy `app.py`]**

Old on the left, new on the right. Same business, modern platform.

> **Real AWS Transform:** this is the transformation and containerization family: .NET porting, Java and Python upgrades,
> mainframe reimagining, and AWS Transform custom for your own patterns. The benefit is that the repetitive 80% is done
> consistently, so your experts spend their time on the 20% that matters.

**[CLICK: ✅ Approve & continue]**

### 29:00 · Agent 4: Terraform (4 min)

Now the target infrastructure.

**[Watch: `get_blueprint` → `set_variables` → `terraform_validate`]**

This is the same pattern again. The agent fetches a **validated blueprint**: VPC, EKS, ECR and a private PostgreSQL. Then, in a single step, it maps
our specification onto it: Graviton nodes, minimum two replicas for the SLO, a Multi-AZ database for the "survive an AZ" goal, and an image registry with scanning.

Then it doesn't *claim* the code is valid. It **proves** it, with a real `terraform validate` plus our own checks against the specification.

**[Result: about 15 seconds, ✅ valid]**

This is the exact task where every model failed from scratch, and where every model succeeds when it's grounded.

Notice one detail: Kubernetes version **1.33**, straight from the spec. Keep that in mind.

> **Real AWS Transform:** network and infrastructure conversion to infrastructure as code, in CloudFormation, CDK or Terraform.
> The benefit is a landing zone in days instead of a quarter of hand-written templates.

**[CLICK: ✅ Approve & continue]**

### 33:00 · Agent 5: Kubernetes validation + live deploy (4 min)

Now the moment of truth. Does it actually run?

To make it realistic, I'm simulating something that happens in *every* real project: someone hand-edits the manifests.
A `latest` tag, missing limits, a writable filesystem, and no disruption budget. The UI labels these clearly as simulated.

**[Watch: policy check finds the violations → `edit_file` fixes → re-check → `deploy_to_kind` → smoke tests]**

It detected all four, fixed all four, validated again, then built the image, deployed it to a real Kubernetes cluster on this laptop,
and ran smoke tests. 200, 200, 200. Order created: 201. Oversell attempt: **409**. The business rule survived the migration.

**[TERMINAL]**
```bash
kubectl --context kind-transform-workshop -n inventory get pods,svc,hpa,pdb
```

That's our 2017 server, now a microservice on Kubernetes.

> **Real AWS Transform:** validation is part of every job, through tests, test environments and cutover checks. The benefit:
> you don't find out in production.

**[CLICK: ✅ Approve & continue]**

### 37:00 · Agent 6: FinOps (3 min)

And finally, the question every CFO asks. What does it cost?

**[Result: about 10 seconds, with the chart]**

**[PAUSE, let the chart land]**

Today: about **$72** a month. As designed: about **$623**. That's *over* our $450 budget.

Why? **[CLICK: open the FO-01 recommendation]**
Remember Kubernetes 1.33? It's now in EKS **extended support**. That's about **$365 a month** that the spec, the IaC agent
and probably most humans in this room would have missed. The FinOps agent caught it.

Upgrade the version, right-size the pods and nodes, and add commitments, and we land at about **$208**: inside the budget.

And I want to be honest about one thing, because the agent is: the new world costs about three times more than one old server.
What you pay for is the 99.9% SLO, a database that survives an AZ, autoscaling, and the critical security finding fixed.
**Modernization isn't always cheaper. It should always be a conscious decision.** Every number here comes from tested code, not from the model.

> **Real AWS Transform:** assessment and business case, so you know the cost *before* you migrate, not after the first invoice.

**[CLICK: ✅ Approve & continue → wrap-up message]**

**[PAUSE]**

Let's recap. One legacy server. Six agents. About two minutes of machine time. Seventeen findings, twenty-four
requirements traced, a tested microservice, validated infrastructure, a live deployment, and a cost model that caught a
$365-a-month mistake. And a human approved every single step.

> **Optional, if you're ahead of time (+3 min):** switch the sidebar to **AgentCore Runtime** and re-run Discovery. "Same code,
> now running serverless on AgentCore." Show **CloudWatch → GenAI Observability**: every tool call is a span.

---

## Part 3: What it means (40:00–52:00)

**[SLIDE 11: "Five things we learned by building it ourselves"]**

We built this so we could understand it. Here's what we learned. I think these five ideas matter more than any feature list.

### 1. Grounding beats generation (40:00–42:30)

**[SLIDE 12]**

Models are brilliant at language and reasoning, and unreliable at remembering the exact version of an API.
Left alone, they write confident, outdated code. Grounded in curated knowledge, they're fast and correct.

That's the real value of a product like AWS Transform. It's not "a model that knows migrations". It's **curated transformation
knowledge**, built and maintained at scale, with a model on top. You *could* build that knowledge yourself, for every
framework, every version and every target. We built it for one small app. It was a lot of work.

### 2. Tools do the counting; models make the call (42:30–44:30)

**[SLIDE 13]**

In our demo the model never adds up a number and never claims a test passed. Deterministic tools scan, parse, validate,
price and test. The model decides, adapts and explains. That's why you can audit every result, and that's the question to ask of any AI tool:
*where does the evidence come from?*

### 3. The human in the loop is a feature, not a limitation (44:30–46:30)

**[SLIDE 14]**

Every agent stopped and waited for me. That isn't because AI isn't ready. It's because **migration decisions are business decisions**:
how much risk, how much cost, which trade-off. Agents remove the toil. They shouldn't remove the accountability.
AWS Transform is designed the same way: agents propose, people approve.

### 4. Validation is non-negotiable (46:30–48:30)

**[SLIDE 15]**

The most valuable moments in the demo weren't the generated files. They were the **checks**: the tests that
caught a bug, `terraform validate`, the policy checks, the 409 on the oversell. An agent that generates without validating is
a very fast way to create technical debt. Look for the validation loop in any tool you adopt.

### 5. Cost is part of the design, not an afterthought (48:30–50:00)

**[SLIDE 16]**

The FinOps agent found a $365-a-month mistake that nobody had asked it to look for. Cost belongs *inside* the migration loop,
next to security and reliability, not in a spreadsheet three months after go-live.

### So why AWS Transform? (50:00–52:00)

**[SLIDE 17: "Build what makes you unique. Use what's already built."]**

You might be thinking: "If I can build this with AgentCore, why do I need AWS Transform?"

**[PAUSE]**

Because we built a *demo* of the mechanism for **one** application. Your reality is hundreds of applications, dozens of
frameworks, mainframes, VMware clusters, .NET estates, years of drift, and a deadline.

AWS Transform gives you that loop **ready, managed and at scale**:
- the curated knowledge, which is the part that's hardest to build;
- the agents, the validation and the human approval workflow, already integrated;
- breadth from day one across assessment, VMware, Windows and .NET, mainframe and language upgrades;
- and for the things that are unique to you, AWS Transform custom lets you define your own transformations, and
  Amazon Bedrock AgentCore, which is what we used today, lets you build your own agents around it.

**Use AWS Transform for the migration factory. Use AgentCore for what makes you different.**
And now that you've seen inside the box, you know exactly what to expect from both, and what to verify.

---

## Call to action (52:00–55:00)

**[SLIDE 18: "On Monday"]**

Three things for Monday morning:

1. **Find your server that nobody wants to touch.** Pick one application, not the whole portfolio.
2. **Run it through AWS Transform**, starting with assessment. Look at the findings, the plan and the business case with
   the questions from today in mind: where does the evidence come from? Where's the validation? Where do I approve?
3. **Clone this repository.** Run the six agents yourself, break them, change the blueprints, plug in your own model.
   The best way to trust an agent is to understand one.

**[SLIDE 19: repository QR code and link]**

The server nobody wanted to touch took two minutes of machine time, and every decision stayed with a human.
Imagine what your team does with the time it gets back.

Thank you.

**[PAUSE → open Q&A]**

---

## Q&A preparation (55:00–60:00)

| Likely question | Suggested answer |
|---|---|
| "Was that AWS Transform?" | No, and that's the point. It's a transparent re-creation of the kind of loop its agents run, built on AgentCore so you can see inside. AWS Transform is the managed, curated, at-scale version. |
| "Which models did you use? Why not GPT or Claude?" | Amazon Nova 2 Lite for five agents and DeepSeek V3.2 for code, all on Bedrock. We benchmarked several and picked per job. AgentCore is model-agnostic, so you can swap models with one environment variable. The design (grounding plus validation) matters more than the model. |
| "Does it work for big, messy apps?" | Our demo is one small service, deliberately. At portfolio scale you need curated knowledge for many stacks, plus dependency mapping and wave planning. That's exactly what AWS Transform brings. |
| "What did the run cost?" | About 250,000 tokens for the whole pipeline: cents. The AgentCore runtimes cost nothing when idle. |
| "Is my code or data safe?" | In our build, everything runs in our own AWS account: Bedrock, AgentCore, and nothing leaves it. Model-written tests run without access to cloud credentials. For AWS Transform, refer to its documentation on data handling and security. |
| "Why did the target cost more than the old server?" | It buys high availability, a managed Multi-AZ database, autoscaling and the security fixes. The agent shows that honestly. Sometimes the right answer is rehost, or retire. |
| "Can the agents deploy to production?" | They could, but they shouldn't without a human. In the demo the human approves every step, and the EKS Terraform is validated, not applied. |
| "How long did this take to build?" | Be honest with your own number. The key point: most of the effort went into curated blueprints and validators, not prompts. |

---

## Cheat sheet: numbers from the rehearsal run (2026-10-20)

| Stage | Time | Number to quote |
|---|---|---|
| Discovery | ~10 s | 17 findings (3 critical, SQL injection), readiness 35–50 (varies per run), replatform |
| Requirements | ~15 s, revision ~8 s | 24 requirements (13 traced to findings), budget $450, 3 open questions; your feedback resolves 2 |
| Modernization | ~60 s | 23 generated tests pass; API contract preserved |
| Terraform | ~15 s | `terraform validate` passes; Graviton, Multi-AZ RDS, Kubernetes 1.33 |
| K8s Validation | ~15 s | 4 simulated violations detected and fixed; live deploy; 200/200/200/201/409 |
| FinOps | ~11 s | $72 today → $623 as designed → $208 optimized; 1.33 extended support +$365/month |
| Whole pipeline | ~2 min | About 250k tokens, a few cents |

## Suggested slide list

1. "Nobody wants to touch the server." · 2. Title · 3. Migrations fail because of knowledge · 4. Migration debt ·
5. AWS Transform capabilities · 6. Over-trust vs under-trust · 7. So we opened the box · 8. The six-agent pipeline ·
9. One thing we learned · 10. Grounding beats generation · *(demo)* · 11. Five lessons · 12–16. One slide per lesson ·
17. Build what's unique, use what's built · 18. On Monday · 19. QR code and repository link
