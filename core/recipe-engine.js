// SPDX-License-Identifier: GPL-3.0-or-later
// core/recipe-engine.js - Skills/Recipe system for autonomous agents
// Provides a declarative way to define agent workflows as "recipes"
// Each recipe is a sequence of steps with conditions, retries, and fallbacks

class RecipeEngine {
  constructor(bridge) {
    this.bridge = bridge;
    this.recipes = new Map();
    this.runningRecipes = new Map();
    this.stepHandlers = new Map();
    this.registerBuiltinSteps();
  }

  // Register a recipe (workflow definition)
  registerRecipe(name, recipe) {
    this.validateRecipe(recipe);
    this.recipes.set(name, recipe);
    console.log(`[RecipeEngine] Registered recipe: ${name} (${recipe.steps.length} steps)`);
  }

  // Validate recipe structure
  validateRecipe(recipe) {
    if (!recipe.name || !recipe.steps || !Array.isArray(recipe.steps)) {
      throw new Error('Recipe must have name and steps array');
    }
    recipe.steps.forEach((step, i) => {
      if (!step.type) throw new Error(`Step ${i} missing type`);
      if (!this.stepHandlers.has(step.type) && !step.custom) {
        console.warn(`[RecipeEngine] Step ${i} type "${step.type}" has no builtin handler, requires custom handler`);
      }
    });
  }

  // Execute a recipe
  async executeRecipe(name, context = {}) {
    const recipe = this.recipes.get(name);
    if (!recipe) throw new Error(`Recipe not found: ${name}`);

    const runId = `${name}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const runContext = {
      runId,
      recipeName: name,
      stepResults: {},
      variables: { ...recipe.variables, ...context },
      startTime: Date.now(),
      status: 'running',
      currentStep: 0
    };

    this.runningRecipes.set(runId, runContext);

    try {
      for (let i = 0; i < recipe.steps.length; i++) {
        runContext.currentStep = i;
        const step = recipe.steps[i];

        // Check condition
        if (step.condition && !this.evaluateCondition(step.condition, runContext)) {
          console.log(`[RecipeEngine] Step ${i} (${step.type}) skipped - condition false`);
          continue;
        }

        // Execute with retries
        let result = await this.executeStepWithRetry(step, runContext, recipe);
        runContext.stepResults[step.id || i] = result;

        // Handle step output mapping
        if (step.output) {
          this.mapOutput(step.output, result, runContext);
        }

        // Check for early exit
        if (step.exitOn && this.evaluateCondition(step.exitOn, runContext)) {
          console.log(`[RecipeEngine] Early exit at step ${i}`);
          break;
        }
      }

      runContext.status = 'completed';
      runContext.endTime = Date.now();
      console.log(`[RecipeEngine] Recipe ${name} completed in ${runContext.endTime - runContext.startTime}ms`);
      return runContext;

    } catch (error) {
      runContext.status = 'failed';
      runContext.error = error.message;
      runContext.endTime = Date.now();
      console.error(`[RecipeEngine] Recipe ${name} failed:`, error);
      throw error;
    } finally {
      this.runningRecipes.delete(runId);
    }
  }

  // Execute single step with retry logic
  async executeStepWithRetry(step, runContext, recipe) {
    const maxRetries = step.retries ?? 3;
    const retryDelay = step.retryDelay ?? 1000;
    let lastError;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        return await this.executeStep(step, runContext);
      } catch (error) {
        lastError = error;
        if (attempt < maxRetries) {
          console.warn(`[RecipeEngine] Step ${step.type} attempt ${attempt + 1} failed, retrying in ${retryDelay}ms:`, error.message);
          await this.sleep(retryDelay * (attempt + 1)); // exponential backoff
        }
      }
    }

    // All retries failed - check fallback
    if (step.fallback) {
      console.log(`[RecipeEngine] Step ${step.type} failed, executing fallback`);
      return await this.executeStep(step.fallback, runContext);
    }

    throw lastError;
  }

  // Execute a single step
  async executeStep(step, runContext) {
    const handler = this.stepHandlers.get(step.type);
    if (!handler) {
      if (step.custom) {
        return await step.custom(step, runContext, this.bridge);
      }
      throw new Error(`No handler for step type: ${step.type}`);
    }

    // Resolve step arguments from context
    const args = this.resolveArgs(step.args || {}, runContext);

    // Execute with timeout
    const timeout = step.timeout || 30000;
    return await Promise.race([
      handler(args, runContext, this.bridge),
      this.sleep(timeout).then(() => { throw new Error(`Step ${step.type} timed out after ${timeout}ms`); })
    ]);
  }

  // Resolve arguments with variable substitution
  resolveArgs(args, runContext) {
    const resolved = {};
    for (const [key, value] of Object.entries(args)) {
      if (typeof value === 'string' && value.startsWith('${') && value.endsWith('}')) {
        const varPath = value.slice(2, -1);
        resolved[key] = this.getVariable(varPath, runContext);
      } else {
        resolved[key] = value;
      }
    }
    return resolved;
  }

  // Get variable from context (supports dot notation)
  getVariable(path, runContext) {
    if (typeof path !== 'string') return undefined;
    const parts = path.split('.');
    let obj = runContext.variables;
    for (const part of parts) {
      if (obj && typeof obj === 'object' && part in obj) {
        obj = obj[part];
      } else {
        // Check step results
        obj = runContext.stepResults;
        for (const p of parts) {
          if (obj && typeof obj === 'object' && p in obj) obj = obj[p];
          else return undefined;
        }
        return obj;
      }
    }
    return obj;
  }

  // Evaluate condition
  evaluateCondition(condition, runContext) {
    if (typeof condition === 'function') return condition(runContext);
    if (typeof condition === 'string') {
      // Simple expression: "var > 5" or "stepResult.success == true"
      try {
        // Very basic eval - in production use a safe expression evaluator
        return new Function('ctx', `with(ctx) return ${condition}`)(runContext);
      } catch {
        return false;
      }
    }
    return Boolean(condition);
  }

  // Map step output to variables
  mapOutput(outputMap, result, runContext) {
    for (const [varName, sourcePath] of Object.entries(outputMap)) {
      let value = result;
      for (const part of sourcePath.split('.')) {
        value = value?.[part];
      }
      runContext.variables[varName] = value;
    }
  }

  // Sleep utility
  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  // Register custom step handler
  registerStepHandler(type, handler) {
    this.stepHandlers.set(type, handler);
  }

  // Get recipe status
  getRecipeStatus(runId) {
    return this.runningRecipes.get(runId) || null;
  }

  // List all recipes
  listRecipes() {
    return Array.from(this.recipes.entries()).map(([name, recipe]) => ({
      name,
      description: recipe.description,
      steps: recipe.steps.length,
      variables: Object.keys(recipe.variables || {})
    }));
  }

  // Built-in step handlers
  registerBuiltinSteps() {
    // Bridge tool call
    this.registerStepHandler('call_tool', async (args, ctx, bridge) => {
      return await bridge.callTool(args.name, args.arguments, args.server);
    });

    // HTTP request
    this.registerStepHandler('http', async (args) => {
      const { url, method = 'GET', headers = {}, body, timeout = 10000 } = args;
      const controller = new AbortController();
      const id = setTimeout(() => controller.abort(), timeout);
      try {
        const response = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined, signal: controller.signal });
        const data = await response.json().catch(() => response.text());
        return { ok: response.ok, status: response.status, data };
      } finally {
        clearTimeout(id);
      }
    });

    // Wait/delay
    this.registerStepHandler('wait', async (args) => {
      await this.sleep(args.ms || 1000);
      return { ok: true };
    });

    // Conditional branch
    this.registerStepHandler('branch', async (args, ctx) => {
      const condition = this.evaluateCondition(args.condition, ctx);
      return { ok: true, branch: condition ? 'then' : 'else' };
    });

    // Parallel execution
    this.registerStepHandler('parallel', async (args, ctx) => {
      const steps = args.steps || [];
      const results = await Promise.allSettled(
        steps.map(s => this.executeStep(s, ctx))
      );
      return {
        ok: true,
        results: results.map((r, i) => ({
          step: i,
          status: r.status,
          value: r.status === 'fulfilled' ? r.value : r.reason?.message
        }))
      };
    });

    // Loop/foreach
    this.registerStepHandler('loop', async (args, ctx) => {
      const items = this.getVariable(args.items, ctx) || [];
      const stepTemplate = args.step;
      const results = [];

      for (let i = 0; i < items.length; i++) {
        ctx.variables.loopIndex = i;
        ctx.variables.loopItem = items[i];
        const step = { ...stepTemplate, args: this.resolveArgs(stepTemplate.args || {}, ctx) };
        const result = await this.executeStep(step, ctx);
        results.push(result);
        if (args.breakOn && this.evaluateCondition(args.breakOn, ctx)) break;
      }

      return { ok: true, iterations: results.length, results };
    });

    // Transform data
    this.registerStepHandler('transform', async (args, ctx) => {
      const input = this.getVariable(args.from, ctx);
      if (typeof args.fn === 'function') {
        return { ok: true, data: args.fn(input, ctx) };
      }
      // Simple transforms
      switch (args.operation) {
        case 'filter': return { ok: true, data: input?.filter(x => this.evaluateCondition(args.predicate, { ...ctx, variables: { item: x } })) || [] };
        case 'map': {
          const arr = Array.isArray(input) ? input : (input && typeof input === 'object' ? Object.values(input) : []);
          return { ok: true, data: arr.map(x => this.resolveArgs(args.mapper, { ...ctx, variables: { item: x } })) };
        }
        case 'reduce': return { ok: true, data: input?.reduce((acc, x) => this.resolveArgs(args.reducer, { ...ctx, variables: { acc, item: x } }), args.initial) };
        default: return { ok: true, data: input };
      }
    });

    // Log/debug
    this.registerStepHandler('log', async (args, ctx) => {
      const message = args.message?.replace(/\$\{(\w+)\}/g, (_, k) => this.getVariable(k, ctx) ?? '');
      console.log(`[RecipeEngine] ${message}`);
      return { ok: true };
    });

    // Set variable
    this.registerStepHandler('set', async (args, ctx) => {
      ctx.variables[args.name] = args.value;
      return { ok: true };
    });
  }
}

// Predefined recipes for common agent workflows
const BUILTIN_RECIPES = {
  // Autonomous code review workflow
  'code-review': {
    name: 'code-review',
    description: 'Autonomous code review: fetch diff, analyze, report',
    variables: { repoPath: '.', baseBranch: 'main' },
    steps: [
      { id: 'git_diff', type: 'call_tool', args: { name: 'git_diff', arguments: { path: '${repoPath}', staged: false } }, output: { diff: 'text' } },
      { id: 'analyze', type: 'transform', args: { from: 'diff', operation: 'map', mapper: { issue: '${item}', severity: 'medium' } }, output: { issues: 'data' } },
      { id: 'report', type: 'log', args: { message: 'Found ${issues.length} issues in ${repoPath}' } }
    ]
  },

  // Multi-provider query with fallback
  'multi-provider-query': {
    name: 'multi-provider-query',
    description: 'Query multiple AI providers with automatic fallback',
    variables: { providers: ['chatgpt', 'deepseek', 'gemini', 'qwen'], question: '' },
    steps: [
      { id: 'loop_providers', type: 'loop', args: { items: '${providers}', step: { type: 'http', args: { url: 'https://api.${loopItem}.com/v1/chat/completions', method: 'POST', body: { model: '${loopItem}', messages: [{ role: 'user', content: '${question}' }] } }, breakOn: 'stepResult.ok == true' } }, output: { responses: 'results' } },
      { id: 'select_best', type: 'transform', args: { from: 'responses', operation: 'filter', predicate: 'item.value.ok == true' }, output: { valid: 'data' } },
      { id: 'return_first', type: 'transform', args: { from: 'valid', operation: 'map', mapper: 'item.value.data' }, output: { answer: 'data[0]' } }
    ]
  },

  // Agent swarm coordination
  'agent-swarm': {
    name: 'agent-swarm',
    description: 'Coordinate multiple agents to solve a task',
    variables: { task: '', agents: ['planner', 'coder', 'reviewer', 'verifier'] },
    steps: [
      { id: 'plan', type: 'call_tool', args: { name: 'call_tool', arguments: { name: 'plan', arguments: { task: '${task}' }, server: 'coordination' } }, output: { plan: 'text' } },
      { id: 'execute_parallel', type: 'parallel', args: { steps: [ { type: 'call_tool', args: { name: 'call_tool', arguments: { name: 'implement', arguments: { plan: '${plan}' }, server: 'coder' } } }, { type: 'call_tool', args: { name: 'call_tool', arguments: { name: 'review', arguments: { plan: '${plan}' }, server: 'reviewer' } } } ] } },
      { id: 'verify', type: 'call_tool', args: { name: 'call_tool', arguments: { name: 'verify', arguments: { plan: '${plan}' }, server: 'verifier' } }, output: { verification: 'text' } }
    ]
  }
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { RecipeEngine, BUILTIN_RECIPES };
}