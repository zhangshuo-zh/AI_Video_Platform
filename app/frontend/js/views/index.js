/** 视图注册表：新增一页就在这里加一行。key 必须和 modules.js 的 id 一致。 */
import overview from './overview.js';
import projects from './projects.js';
import assets from './assets.js';
import writing from './writing.js';
import generate from './generate.js';
import pipeline from './pipeline.js';
import composition from './composition.js';
import audio from './audio.js';
import review from './review.js';
import knowledge from './knowledge.js';
import systemConfig from './system-config.js';

export default { overview, projects, assets, writing, generate, pipeline, composition, audio, review, knowledge, 'system-config': systemConfig };
