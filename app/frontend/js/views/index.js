/** 视图注册表：新增一页就在这里加一行。key 必须和 modules.js 的 id 一致。 */
import overview from './overview.js';
import production from './production.js';
import projects from './projects.js';
import shotWorkbench from './shot-workbench.js';
import assets from './assets.js';
import writing from './writing.js';
import generate from './generate.js';
import pipeline from './pipeline.js';
import composition from './composition.js';
import audio from './audio.js';
import review from './review.js';
import knowledge from './knowledge.js';
import agents from './agents.js';
import models from './models.js';
import systemConfig from './system-config.js';

export default { overview, production, projects, shot: shotWorkbench, assets, writing, generate, pipeline, composition, audio, review, knowledge, agents, models, 'system-config': systemConfig };
