// SPDX-License-Identifier: GPL-3.0-or-later
// core/coordination-hub.js - Multi-provider A2A coordination hub with quota auto-switch
// Manages connections to multiple AI providers, handles quota/rate limits,
// automatically switches providers on exhaustion, and coordinates agent-to-agent communication

class ProviderManager {
  constructor() {
    this.providers = new Map();
    this.currentProvider = null;
    this.usage = new Map(); // provider -> { requests, tokens, errors, lastReset }
    this.quotas = new Map(); // provider -> { requestsPerMinute, tokensPerMinute, maxConcurrent }
    this.health = new Map(); // provider -> { status, latency, lastCheck }
    this.callbacks = new Set();
  }

  // Register a provider
  registerProvider(name, config) {
    this.providers.set(name, {
      name,
      endpoint: config.endpoint,
      apiKey: config.apiKey,
      models: config.models || ['default'],
      auth: config.auth || 'bearer',
      headers: config.headers || {},
      transformRequest: config.transformRequest || ((req) => req),
      transformResponse: config.transformResponse || ((res) => res),
      maxRetries: config.maxRetries || 3,
      timeout: config.timeout || 30000
    });

    this.quotas.set(name, {
      requestsPerMinute: config.requestsPerMinute || 60,
      tokensPerMinute: config.tokensPerMinute || 100000,
      maxConcurrent: config.maxConcurrent || 5
    });

    this.usage.set(name, {
      requests: 0,
      tokens: 0,
      errors: 0,
      concurrent: 0,
      lastReset: Date.now()
    });

    this.health.set(name, {
      status: 'unknown',
      latency: 0,
      lastCheck: 0,
      consecutiveFailures: 0
    });

    console.log(`[CoordinationHub] Registered provider: ${name}`);
    this.emit('provider-registered', { name, config });
  }

  // Get best available provider for a request
  getBestProvider(requirements = {}) {
    const candidates = Array.from(this.providers.entries())
      .filter(([name, provider]) => this.isProviderHealthy(name))
      .filter(([name]) => this.hasQuota(name, requirements))
      .map(([name, provider]) => ({
        name,
        provider,
        health: this.health.get(name),
        usage: this.usage.get(name),
        score: this.calculateScore(name, requirements)
      }))
      .sort((a, b) => b.score - a.score);

    if (candidates.length === 0) {
      // Fallback: return any provider even if over quota
      const fallback = Array.from(this.providers.entries())
        .filter(([name]) => this.isProviderHealthy(name))
        .sort((a, b) => this.health.get(a[0]).latency - this.health.get(b[0]).latency)[0];
      if (fallback) {
        console.warn(`[CoordinationHub] All providers over quota, using fallback: ${fallback[0]}`);
        return fallback[0];
      }
      throw new Error('No healthy providers available');
    }

    return candidates[0].name;
  }

  // Check if provider is healthy
  isProviderHealthy(name) {
    const health = this.health.get(name);
    if (!health) return false;
    if (health.status === 'down') return false;
    if (health.consecutiveFailures >= 5) return false;
    return true;
  }

  // Check quota availability
  hasQuota(name, requirements) {
    const quota = this.quotas.get(name);
    const usage = this.usage.get(name);
    if (!quota || !usage) return false;

    // Reset counters if minute passed
    const now = Date.now();
    if (now - usage.lastReset > 60000) {
      usage.requests = 0;
      usage.tokens = 0;
      usage.lastReset = now;
    }

    const estimatedTokens = requirements.estimatedTokens || 1000;
    return usage.requests < quota.requestsPerMinute &&
           usage.tokens + estimatedTokens < quota.tokensPerMinute &&
           usage.concurrent < quota.maxConcurrent;
  }

  // Calculate provider score (higher = better)
  calculateScore(name, requirements) {
    const health = this.health.get(name);
    const usage = this.usage.get(name);
    const quota = this.quotas.get(name);

    let score = 100;

    // Prefer lower latency
    score -= Math.min(health.latency, 5000) / 50;

    // Prefer more available quota
    const quotaUsage = usage.requests / quota.requestsPerMinute;
    score -= quotaUsage * 30;

    // Prefer fewer errors
    score -= usage.errors * 5;

    // Prefer providers that match requirements
    if (requirements.model) {
      const provider = this.providers.get(name);
      if (provider.models.includes(requirements.model)) score += 20;
    }

    return score;
  }

  // Execute request with automatic provider selection and fallback
  async execute(request, options = {}) {
    const { model, estimatedTokens, maxRetries = 3 } = options;
    let lastError;
    let providerName = this.currentProvider || this.getBestProvider({ model, estimatedTokens });

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      const provider = this.providers.get(providerName);
      if (!provider) {
        lastError = new Error(`Provider not found: ${providerName}`);
        providerName = this.getBestProvider({ model, estimatedTokens, exclude: providerName });
        continue;
      }

      this.usage.get(providerName).concurrent++;
      const startTime = Date.now();

      try {
        const transformedRequest = provider.transformRequest(request);
        const response = await this.callProvider(provider, transformedRequest);
        const transformedResponse = provider.transformResponse(response);

        // Update metrics
        const latency = Date.now() - startTime;
        this.recordSuccess(providerName, latency, response.tokens || estimatedTokens || 1000);
        this.currentProvider = providerName; // Stick with working provider

        return { provider: providerName, response: transformedResponse, latency };

      } catch (error) {
        this.recordFailure(providerName, error);
        lastError = error;

        // Check if we should switch providers
        if (this.shouldSwitchProvider(providerName, error)) {
          console.log(`[CoordinationHub] Switching from ${providerName} due to: ${error.message}`);
          providerName = this.getBestProvider({ model, estimatedTokens, exclude: providerName });
        } else if (attempt < maxRetries) {
          // Retry same provider with backoff
          await this.sleep(1000 * Math.pow(2, attempt));
        }
      } finally {
        this.usage.get(providerName).concurrent--;
      }
    }

    throw lastError || new Error('All providers exhausted');
  }

  // Call provider API
  async callProvider(provider, request) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), provider.timeout);

    try {
      const headers = {
        'Content-Type': 'application/json',
        ...provider.headers
      };

      if (provider.apiKey) {
        if (provider.auth === 'bearer') {
          headers['Authorization'] = `Bearer ${provider.apiKey}`;
        } else if (provider.auth === 'api-key') {
          headers['X-API-Key'] = provider.apiKey;
        }
      }

      const response = await fetch(provider.endpoint, {
        method: 'POST',
        headers,
        body: JSON.stringify(request),
        signal: controller.signal
      });

      if (!response.ok) {
        const errorText = await response.text().catch(() => '');
        throw new Error(`HTTP ${response.status}: ${errorText}`);
      }

      return await response.json();

    } finally {
      clearTimeout(timeoutId);
    }
  }

  // Record successful request
  recordSuccess(name, latency, tokens) {
    const usage = this.usage.get(name);
    const health = this.health.get(name);

    usage.requests++;
    usage.tokens += tokens;
    usage.errors = Math.max(0, usage.errors - 1); // Decay errors

    health.latency = (health.latency * 0.9) + (latency * 0.1); // EMA
    health.consecutiveFailures = 0;
    health.status = 'healthy';
    health.lastCheck = Date.now();

    this.emit('request-complete', { provider: name, latency, tokens });
  }

  // Record failed request
  recordFailure(name, error) {
    const usage = this.usage.get(name);
    const health = this.health.get(name);

    usage.errors++;
    health.consecutiveFailures++;
    health.lastCheck = Date.now();

    if (health.consecutiveFailures >= 3) {
      health.status = 'degraded';
    }
    if (health.consecutiveFailures >= 5) {
      health.status = 'down';
    }

    this.emit('request-failed', { provider: name, error: error.message });
  }

  // Determine if should switch provider
  shouldSwitchProvider(name, error) {
    const health = this.health.get(name);
    const usage = this.usage.get(name);
    const quota = this.quotas.get(name);

    // Switch on rate limit / quota exceeded
    if (error.message.includes('429') || error.message.includes('rate limit') ||
        error.message.includes('quota') || error.message.includes('capacity')) {
      return true;
    }

    // Switch on repeated failures
    if (health.consecutiveFailures >= 3) return true;

    // Switch if over quota
    if (usage.requests >= quota.requestsPerMinute * 0.9) return true;

    return false;
  }

  // Health check all providers
  async healthCheckAll() {
    const checks = Array.from(this.providers.entries()).map(async ([name, provider]) => {
      const start = Date.now();
      try {
        // Simple health check request
        await fetch(provider.endpoint.replace('/chat/completions', '/models'), {
          method: 'GET',
          headers: { 'Authorization': `Bearer ${provider.apiKey}` },
          signal: AbortSignal.timeout(5000)
        });
        this.health.get(name).status = 'healthy';
        this.health.get(name).latency = Date.now() - start;
      } catch {
        this.health.get(name).status = 'down';
        this.health.get(name).consecutiveFailures++;
      }
      this.health.get(name).lastCheck = Date.now();
    });

    await Promise.allSettled(checks);
    this.emit('health-check-complete', Object.fromEntries(this.health));
  }

  // Get status of all providers
  getStatus() {
    return {
      currentProvider: this.currentProvider,
      providers: Array.from(this.providers.entries()).map(([name, p]) => ({
        name,
        models: p.models,
        health: this.health.get(name),
        usage: this.usage.get(name),
        quota: this.quotas.get(name)
      }))
    };
  }

  // Event system
  on(event, callback) {
    this.callbacks.add({ event, callback });
  }

  emit(event, data) {
    for (const cb of this.callbacks) {
      if (cb.event === event || cb.event === '*') {
        try { cb.callback(data); } catch (e) { console.error('Callback error:', e); }
      }
    }
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

// A2A (Agent-to-Agent) Communication Protocol
class A2AProtocol {
  constructor(providerManager) {
    this.providerManager = providerManager;
    this.agents = new Map(); // agentId -> { capabilities, endpoint, status }
    this.messageQueue = [];
    this.handlers = new Map(); // messageType -> handler
    this.registerBuiltinHandlers();
  }

  // Register an agent
  registerAgent(agentId, capabilities, endpoint) {
    this.agents.set(agentId, {
      id: agentId,
      capabilities,
      endpoint,
      status: 'idle',
      lastSeen: Date.now(),
      messageCount: 0
    });
    console.log(`[A2A] Registered agent: ${agentId} (${capabilities.join(', ')})`);
  }

  // Send message to agent
  async sendMessage(toAgentId, message, options = {}) {
    const agent = this.agents.get(toAgentId);
    if (!agent) throw new Error(`Agent not found: ${toAgentId}`);

    const envelope = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      from: options.from || 'coordination-hub',
      to: toAgentId,
      type: message.type,
      payload: message.payload,
      timestamp: Date.now(),
      correlationId: options.correlationId,
      replyTo: options.replyTo,
      timeout: options.timeout || 30000
    };

    agent.messageCount++;
    agent.lastSeen = Date.now();

    // Route via provider if agent has endpoint
    if (agent.endpoint) {
      return await this.routeViaProvider(agent, envelope);
    }

    // Local handler
    return await this.handleLocal(envelope);
  }

  // Route message via AI provider
  async routeViaProvider(agent, envelope) {
    const request = {
      model: agent.capabilities.includes('reasoning') ? 'gpt-4' : 'gpt-3.5-turbo',
      messages: [
        { role: 'system', content: `You are agent ${agent.id}. Capabilities: ${agent.capabilities.join(', ')}.` },
        { role: 'user', content: JSON.stringify(envelope) }
      ],
      temperature: 0.3,
      max_tokens: 2000
    };

    const result = await this.providerManager.execute(request, {
      model: request.model,
      estimatedTokens: 3000
    });

    return result.response;
  }

  // Handle message locally
  async handleLocal(envelope) {
    const handler = this.handlers.get(envelope.type);
    if (!handler) {
      return { ok: false, error: `No handler for message type: ${envelope.type}` };
    }

    try {
      return await handler(envelope.payload, envelope);
    } catch (error) {
      return { ok: false, error: error.message };
    }
  }

  // Register message handler
  onMessage(type, handler) {
    this.handlers.set(type, handler);
  }

  // Broadcast to all agents with capability
  async broadcastToCapability(capability, message, options = {}) {
    const targets = Array.from(this.agents.entries())
      .filter(([_, a]) => a.capabilities.includes(capability) && a.status !== 'offline')
      .map(([id]) => id);

    const results = await Promise.allSettled(
      targets.map(id => this.sendMessage(id, message, options))
    );

    return results.map((r, i) => ({
      agent: targets[i],
      status: r.status,
      value: r.status === 'fulfilled' ? r.value : r.reason?.message
    }));
  }

  // Request-response pattern with correlation
  async requestResponse(toAgentId, message, options = {}) {
    const correlationId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    return await this.sendMessage(toAgentId, message, {
      ...options,
      correlationId,
      replyTo: 'coordination-hub'
    });
  }

  // Built-in handlers for coordination
  registerBuiltinHandlers() {
    this.onMessage('ping', async () => ({ ok: true, pong: true }));
    this.onMessage('status', async (_, envelope) => {
      const agent = this.agents.get(envelope.from);
      return { ok: true, status: agent?.status || 'unknown' };
    });
    this.onMessage('capabilities', async (_, envelope) => {
      const agent = this.agents.get(envelope.from);
      return { ok: true, capabilities: agent?.capabilities || [] };
    });
  }

  // Get agent registry
  getAgents() {
    return Array.from(this.agents.values());
  }
}

// Memory optimization for multi-provider
class MemoryOptimizer {
  constructor(providerManager, options = {}) {
    this.providerManager = providerManager;
    this.maxHistoryPerProvider = options.maxHistoryPerProvider || 50;
    this.maxTotalTokens = options.maxTotalTokens || 500000;
    this.compressionThreshold = options.compressionThreshold || 0.7;
    this.histories = new Map(); // provider -> conversation history
    this.compressionEnabled = options.compressionEnabled !== false;
  }

  // Add message to history
  addMessage(provider, message) {
    if (!this.histories.has(provider)) {
      this.histories.set(provider, []);
    }
    const history = this.histories.get(provider);
    history.push({ ...message, timestamp: Date.now() });

    // Trim if too long
    if (history.length > this.maxHistoryPerProvider) {
      history.splice(0, history.length - this.maxHistoryPerProvider);
    }

    // Check total tokens
    this.checkAndCompress();
  }

  // Get history for provider
  getHistory(provider, maxTokens = null) {
    const history = this.histories.get(provider) || [];
    if (!maxTokens) return history;

    // Return recent messages that fit in token budget
    let total = 0;
    const result = [];
    for (let i = history.length - 1; i >= 0; i--) {
      const tokens = this.estimateTokens(history[i]);
      if (total + tokens > maxTokens) break;
      total += tokens;
      result.unshift(history[i]);
    }
    return result;
  }

  // Estimate tokens
  estimateTokens(message) {
    const text = JSON.stringify(message);
    return Math.ceil(text.length / 4); // Rough estimate
  }

  // Check and compress if needed
  checkAndCompress() {
    if (!this.compressionEnabled) return;

    let totalTokens = 0;
    for (const history of this.histories.values()) {
      for (const msg of history) {
        totalTokens += this.estimateTokens(msg);
      }
    }

    if (totalTokens > this.maxTotalTokens * this.compressionThreshold) {
      this.compressOldest();
    }
  }

  // Compress oldest histories
  compressOldest() {
    // Sort providers by last activity
    const providers = Array.from(this.histories.entries())
      .map(([name, history]) => ({
        name,
        history,
        lastActivity: history.length > 0 ? history[history.length - 1].timestamp : 0,
        tokens: history.reduce((sum, m) => sum + this.estimateTokens(m), 0)
      }))
      .sort((a, b) => a.lastActivity - b.lastActivity);

    // Compress oldest 25%
    const toCompress = Math.ceil(providers.length * 0.25);
    for (let i = 0; i < toCompress && i < providers.length; i++) {
      const p = providers[i];
      if (p.history.length > 10) {
        // Keep first 2 (system context) and last 8, summarize middle
        const keepFirst = 2;
        const keepLast = 8;
        const middle = p.history.slice(keepFirst, -keepLast);
        if (middle.length > 0) {
          const summary = this.summarize(middle);
          p.history.splice(keepFirst, middle.length, {
            role: 'system',
            content: `[Compressed ${middle.length} messages: ${summary}]`,
            compressed: true,
            originalCount: middle.length,
            timestamp: Date.now()
          });
          console.log(`[MemoryOptimizer] Compressed ${p.name}: ${middle.length} messages -> 1 summary`);
        }
      }
    }
  }

  // Summarize messages (in production, call an LLM)
  summarize(messages) {
    const topics = new Set();
    const roles = new Set();
    for (const m of messages) {
      roles.add(m.role);
      // Extract key topics (simplified)
      const words = (m.content || '').toLowerCase().match(/\b\w{5,}\b/g) || [];
      words.slice(0, 3).forEach(w => topics.add(w));
    }
    return `Topics: ${Array.from(topics).slice(0, 5).join(', ')} | Roles: ${Array.from(roles).join(', ')}`;
  }

  // Get memory stats
  getStats() {
    let totalMessages = 0;
    let totalTokens = 0;
    const byProvider = {};

    for (const [name, history] of this.histories.entries()) {
      const tokens = history.reduce((sum, m) => sum + this.estimateTokens(m), 0);
      totalMessages += history.length;
      totalTokens += tokens;
      byProvider[name] = { messages: history.length, tokens };
    }

    return { totalMessages, totalTokens, byProvider };
  }
}

// Shared memory: one store, every provider/agent reads the same thing.
// short_term = rolling recent messages per task; long_term = summaries.
class SharedMemory {
  constructor(options = {}) {
    this.statePath = options.statePath || null;
    this.shortLimit = options.shortLimit || 40;
    this.longLimit = options.longLimit || 20;
    this.autoPersist = options.autoPersist !== false;
    this.shortTerm = new Map(); // task -> message[]
    this.longTerm = new Map();  // task -> {summary, count, timestamp}[]
    this.lastTask = null;
    this.lastActivity = 0;
    if (this.statePath) this.load();
  }

  load() {
    try {
      const fs = require('fs');
      const path = require('path');
      if (!fs.existsSync(this.statePath)) return;
      const data = JSON.parse(fs.readFileSync(this.statePath, 'utf8'));
      this.shortTerm = new Map(Object.entries(data.shortTerm || {}));
      this.longTerm = new Map(Object.entries(data.longTerm || {}));
      this.lastTask = data.lastTask || null;
      this.lastActivity = data.lastActivity || 0;
    } catch (e) {
      this.shortTerm = new Map();
      this.longTerm = new Map();
      this.lastTask = null;
      this.lastActivity = 0;
    }
  }

  persist() {
    if (!this.statePath || !this.autoPersist) return;
    try {
      const fs = require('fs');
      const path = require('path');
      fs.mkdirSync(path.dirname(this.statePath), { recursive: true });
      fs.writeFileSync(this.statePath, JSON.stringify({
        shortTerm: Object.fromEntries(this.shortTerm),
        longTerm: Object.fromEntries(this.longTerm),
        lastTask: this.lastTask,
        lastActivity: this.lastActivity
      }, null, 2), 'utf8');
    } catch (e) {
      // disk failure never breaks in-memory operation
    }
  }

  // Append messages to the shared short-term buffer for a task
  record(task, messages) {
    if (!this.shortTerm.has(task)) this.shortTerm.set(task, []);
    const bucket = this.shortTerm.get(task);
    for (const m of messages) bucket.push({ ...m, timestamp: m.timestamp || Date.now() });

    // Evict overflow into long-term summaries
    if (bucket.length > this.shortLimit) {
      const overflow = bucket.splice(0, bucket.length - this.shortLimit);
      this.promote(task, overflow);
    }
    this.lastTask = task;
    this.lastActivity = Date.now();
    this.persist();
  }

  promote(task, overflow) {
    if (!this.longTerm.has(task)) this.longTerm.set(task, []);
    const bucket = this.longTerm.get(task);
    const line = overflow.map(m => `${m.role || '?'}: ${String(m.content || '').slice(0, 160)}`).join(' | ');
    bucket.push({ summary: line, count: overflow.length, timestamp: Date.now() });
    while (bucket.length > this.longLimit) bucket.shift();
  }

  // Inject an external (agent/LLM) long-term summary
  setLongTerm(task, summary) {
    if (!this.longTerm.has(task)) this.longTerm.set(task, []);
    const bucket = this.longTerm.get(task);
    bucket.push({ summary, count: 0, timestamp: Date.now() });
    while (bucket.length > this.longLimit) bucket.shift();
    this.persist();
  }

  getShortTerm(task) {
    return [...(this.shortTerm.get(task) || [])];
  }

  getLongTerm(task) {
    return [...(this.longTerm.get(task) || [])];
  }

  // Messages every provider gets before its own context:
  // 1. last-work redirect, 2. long-term summaries, 3. short-term recents
  bootstrapMessages(task) {
    const out = [];
    if (this.lastTask && this.lastTask !== task) {
      out.push({
        role: 'system',
        content: `[shared-memory] last active work: task '${this.lastTask}'. ` +
                 'Continue from there unless this request is a new task.'
      });
    }
    const longs = this.getLongTerm(task);
    if (longs.length) {
      out.push({
        role: 'system',
        content: `[shared-memory long-term] task '${task}':\n` +
                 longs.map(e => `- ${e.summary}`).join('\n')
      });
    }
    const short = this.getShortTerm(task);
    if (short.length) {
      out.push({ role: 'system', content: '[shared-memory short-term] recent shared context:' });
      out.push(...short);
    }
    return out;
  }

  // Inject shared memory in front, then record the new messages
  prepare(task, messages) {
    const merged = [...this.bootstrapMessages(task), ...messages];
    this.record(task, messages);
    return merged;
  }

  stats() {
    const tasks = [...new Set([...this.shortTerm.keys(), ...this.longTerm.keys()])].sort();
    let shortMessages = 0;
    let longSummaries = 0;
    for (const v of this.shortTerm.values()) shortMessages += v.length;
    for (const v of this.longTerm.values()) longSummaries += v.length;
    return {
      tasks,
      lastTask: this.lastTask,
      shortMessages,
      longSummaries,
      statePath: this.statePath
    };
  }
}

// Main Coordination Hub
class CoordinationHub {
  constructor(config = {}) {
    this.providerManager = new ProviderManager();
    this.a2a = new A2AProtocol(this.providerManager);
    this.memory = new MemoryOptimizer(this.providerManager, config.memory);
    // One shared memory for every provider/agent (cross-provider context)
    this.shared = new SharedMemory({
      statePath: config.sharedMemoryPath || null,
      shortLimit: config.sharedShortLimit,
      longLimit: config.sharedLongLimit
    });
    this.recipeEngine = null; // Set externally if available

    // Register default providers from config
    if (config.providers) {
      for (const [name, providerConfig] of Object.entries(config.providers)) {
        this.providerManager.registerProvider(name, providerConfig);
      }
    }

    // Periodic health checks
    this.healthCheckInterval = setInterval(() => {
      this.providerManager.healthCheckAll();
    }, config.healthCheckInterval || 60000);

    // Periodic memory optimization
    this.memoryInterval = setInterval(() => {
      this.memory.checkAndCompress();
    }, config.memoryCheckInterval || 30000);
  }

  // Initialize with recipe engine
  setRecipeEngine(engine) {
    this.recipeEngine = engine;
  }

  // Prepare a provider request: inject shared memory, then fit the window.
  // Returns { provider, task, messages, strategy, tokensBefore, tokensAfter, compressed, shared }
  prepareRequest(provider, messages, options = {}) {
    const task = options.task || this.shared.lastTask || 'default';
    const merged = this.shared.prepare(task, messages);

    let chosen = provider;
    const pm = this.providerManager;
    if (!pm.providers.has(chosen) || !pm.isProviderHealthy(chosen) || !pm.hasQuota(chosen, {})) {
      try {
        chosen = pm.getBestProvider({ allowOverQuota: options.allowOverQuota });
      } catch (e) {
        // no healthy provider: keep the requested one and let the caller see it
      }
    }

    const tokensBefore = this.memory.estimateTokens({ content: merged.map(m => m.content || '').join('\n') });
    const budget = options.maxTokens || 24000;

    // Head (shared-memory bootstrap) stays; trim oldest middle when over budget
    let out = merged;
    let compressed = false;
    if (tokensBefore > budget) {
      const head = [];
      let i = 0;
      while (i < out.length && out[i].role === 'system' &&
             String(out[i].content || '').startsWith('[shared-memory')) {
        head.push(out[i]);
        i++;
      }
      let tail = out.slice(i);
      let tailTokens = this.memory.estimateTokens({ content: tail.map(m => m.content || '').join('\n') });
      while (tail.length > 1 && tailTokens > budget) {
        tail.shift();
        compressed = true;
        tailTokens = this.memory.estimateTokens({ content: tail.map(m => m.content || '').join('\n') });
      }
      out = head.concat(tail);
    }

    return {
      provider: chosen,
      requested: provider,
      switched: chosen !== provider,
      task,
      messages: out,
      strategy: compressed ? 'sliding' : 'append-stable',
      tokensBefore,
      tokensAfter: this.memory.estimateTokens({ content: out.map(m => m.content || '').join('\n') }),
      compressed,
      shared: this.shared.stats()
    };
  }

  // Execute task with full coordination
  async executeTask(task, options = {}) {
    const { providers, agents, recipe, ...execOptions } = options;

    // Override provider selection if specified
    if (providers) {
      // Filter to specified providers
    }

    // Use recipe engine if available
    if (recipe && this.recipeEngine) {
      return await this.recipeEngine.executeRecipe(recipe, { task, ...execOptions });
    }

    // Direct provider execution
    return await this.providerManager.execute(task, execOptions);
  }

  // Send message between agents
  async sendAgentMessage(toAgentId, message, options = {}) {
    return await this.a2a.sendMessage(toAgentId, message, options);
  }

  // Broadcast to agents with capability
  async broadcast(capability, message, options = {}) {
    return await this.a2a.broadcastToCapability(capability, message, options);
  }

  // Register agent
  registerAgent(agentId, capabilities, endpoint) {
    this.a2a.registerAgent(agentId, capabilities, endpoint);
  }

  // Get system status
  getStatus() {
    return {
      providers: this.providerManager.getStatus(),
      agents: this.a2a.getAgents(),
      memory: this.memory.getStats(),
      shared: this.shared.stats(),
      currentProvider: this.providerManager.currentProvider
    };
  }

  // Shutdown
  shutdown() {
    clearInterval(this.healthCheckInterval);
    clearInterval(this.memoryInterval);
    console.log('[CoordinationHub] Shutdown complete');
  }
}

// Configuration for 4 target platforms
const DEFAULT_PROVIDER_CONFIG = {
  chatgpt: {
    endpoint: 'https://api.openai.com/v1/chat/completions',
    apiKey: process.env.OPENAI_API_KEY || '${OPENAI_API_KEY}',
    models: ['gpt-4', 'gpt-4-turbo', 'gpt-3.5-turbo'],
    requestsPerMinute: 60,
    tokensPerMinute: 150000,
    maxConcurrent: 5
  },
  deepseek: {
    endpoint: 'https://api.deepseek.com/v1/chat/completions',
    apiKey: process.env.DEEPSEEK_API_KEY || '${DEEPSEEK_API_KEY}',
    models: ['deepseek-chat', 'deepseek-coder'],
    requestsPerMinute: 60,
    tokensPerMinute: 200000,
    maxConcurrent: 5
  },
  gemini: {
    endpoint: 'https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent',
    apiKey: process.env.GEMINI_API_KEY || '${GEMINI_API_KEY}',
    auth: 'api-key',
    models: ['gemini-pro', 'gemini-pro-vision'],
    requestsPerMinute: 60,
    tokensPerMinute: 100000,
    maxConcurrent: 3,
    transformRequest: (req) => ({
      contents: req.messages.map(m => ({ role: m.role === 'assistant' ? 'model' : 'user', parts: [{ text: m.content }] })),
      generationConfig: { temperature: req.temperature, maxOutputTokens: req.max_tokens }
    }),
    transformResponse: (res) => ({
      choices: [{ message: { content: res.candidates?.[0]?.content?.parts?.[0]?.text || '' } }],
      usage: { total_tokens: res.usageMetadata?.totalTokenCount || 0 }
    })
  },
  qwen: {
    endpoint: 'https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
    apiKey: process.env.QWEN_API_KEY || '${QWEN_API_KEY}',
    models: ['qwen-max', 'qwen-plus', 'qwen-turbo'],
    requestsPerMinute: 60,
    tokensPerMinute: 100000,
    maxConcurrent: 3,
    transformRequest: (req) => ({
      model: req.model,
      input: { messages: req.messages },
      parameters: { temperature: req.temperature, max_tokens: req.max_tokens }
    }),
    transformResponse: (res) => ({
      choices: [{ message: { content: res.output?.text || res.output?.choices?.[0]?.message?.content || '' } }],
      usage: { total_tokens: res.usage?.total_tokens || 0 }
    })
  }
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    CoordinationHub,
    ProviderManager,
    A2AProtocol,
    MemoryOptimizer,
    SharedMemory,
    DEFAULT_PROVIDER_CONFIG
  };
}