import { relationalStore } from '@kit.ArkData';
import { common } from '@kit.AbilityKit';
import { NamespaceNode } from '../net/ControlPlaneClient';

/**
 * 虚拟目录树本地缓存（H2）
 *
 * relationalStore（SQLite）存储控制面下发的 namespace 节点：
 *   - namespace 表：节点（id 主键，parentId 建索引），INSERT OR REPLACE upsert
 *   - meta 表：syncVersion（增量拉取游标）
 */
export interface NamespaceStat {
  totalNodes: number;
  fileCount: number;
  totalSize: number;
}

const TABLE = 'namespace';
const META_TABLE = 'meta';
const KEY_SYNC_VERSION = 'syncVersion';

export class NamespaceStore {
  private store?: relationalStore.RdbStore;

  async init(context: common.UIAbilityContext): Promise<void> {
    if (this.store !== undefined) {
      return;
    }
    const config: relationalStore.StoreConfig = {
      name: 'storpool.db',
      securityLevel: relationalStore.SecurityLevel.S1,
    };
    this.store = await relationalStore.getRdbStore(context, config);
    await this.store.executeSql(`CREATE TABLE IF NOT EXISTS ${TABLE} (
      id INTEGER PRIMARY KEY,
      parentId INTEGER NOT NULL,
      name TEXT NOT NULL,
      type TEXT NOT NULL,
      size INTEGER NOT NULL DEFAULT 0,
      mtime INTEGER NOT NULL DEFAULT 0,
      version INTEGER NOT NULL DEFAULT 0
    )`);
    await this.store.executeSql(`CREATE INDEX IF NOT EXISTS idx_ns_parent ON ${TABLE}(parentId)`);
    await this.store.executeSql(`CREATE TABLE IF NOT EXISTS ${META_TABLE} (
      k TEXT PRIMARY KEY,
      v TEXT NOT NULL
    )`);
  }

  /** 增量同步游标：上次已同步到的 namespace 全局版本 */
  async getSyncedVersion(): Promise<number> {
    if (this.store === undefined) {
      return 0;
    }
    const rs = await this.store.querySql(`SELECT v FROM ${META_TABLE} WHERE k='${KEY_SYNC_VERSION}'`);
    let v = 0;
    if (rs.goToNextRow()) {
      v = rs.getLong(rs.getColumnIndex('v'));
    }
    rs.close();
    return v;
  }

  async setSyncedVersion(version: number): Promise<void> {
    if (this.store === undefined) {
      throw new Error('NamespaceStore not initialized');
    }
    const vb: relationalStore.ValuesBucket = { 'k': KEY_SYNC_VERSION, 'v': String(version) };
    await this.store.insert(META_TABLE, vb,
      relationalStore.ConflictResolution.ON_CONFLICT_REPLACE);
  }

  /**
   * 应用增量变更（P0-1）：墓碑节点（deleted=true）本地删除，其余 upsert；
   * 游标单调推进——newVersion < 已同步版本时拒绝推进（游标回退保护）。
   * 目录墓碑的级联由服务端负责（tombstone 协议语义：控制面级联下发全部受影响节点）。
   */
  async applyChanges(nodes: NamespaceNode[], newVersion: number): Promise<void> {
    if (this.store === undefined) {
      throw new Error('NamespaceStore not initialized');
    }
    const current = await this.getSyncedVersion();
    const advance = newVersion >= current;
    this.store.beginTransaction();
    try {
      for (const n of nodes) {
        if (n.deleted === true) {
          await this.store.executeSql(`DELETE FROM ${TABLE} WHERE id = ?`, [n.id]);
        } else {
          const vb: relationalStore.ValuesBucket = {
            'id': n.id,
            'parentId': n.parentId,
            'name': n.name,
            'type': n.type,
            'size': n.size,
            'mtime': n.mtime,
            'version': n.version,
          };
          await this.store.insert(TABLE, vb,
            relationalStore.ConflictResolution.ON_CONFLICT_REPLACE);
        }
      }
      if (advance) {
        const vb2: relationalStore.ValuesBucket =
          { 'k': KEY_SYNC_VERSION, 'v': String(newVersion) };
        await this.store.insert(META_TABLE, vb2,
          relationalStore.ConflictResolution.ON_CONFLICT_REPLACE);
      }
      this.store.commit();
    } catch (e) {
      this.store.rollBack();
      throw new Error(`applyChanges failed: ${e}`);
    }
  }

  /**
   * 全量重置（P0-1：服务端版本回退时用）：清空节点表 + 全量 upsert + 游标回退重写。
   * 单事务保证原子性——要么全部生效，要么保持旧状态。
   */
  async resetAll(nodes: NamespaceNode[], newVersion: number): Promise<void> {
    if (this.store === undefined) {
      throw new Error('NamespaceStore not initialized');
    }
    this.store.beginTransaction();
    try {
      await this.store.executeSql(`DELETE FROM ${TABLE}`);
      for (const n of nodes) {
        if (n.deleted === true) {
          continue; // 全量重拉只应含活节点，防御性跳过墓碑
        }
        const vb: relationalStore.ValuesBucket = {
          'id': n.id,
          'parentId': n.parentId,
          'name': n.name,
          'type': n.type,
          'size': n.size,
          'mtime': n.mtime,
          'version': n.version,
        };
        await this.store.insert(TABLE, vb,
          relationalStore.ConflictResolution.ON_CONFLICT_REPLACE);
      }
      const vb2: relationalStore.ValuesBucket =
        { 'k': KEY_SYNC_VERSION, 'v': String(newVersion) };
      await this.store.insert(META_TABLE, vb2,
        relationalStore.ConflictResolution.ON_CONFLICT_REPLACE);
      this.store.commit();
    } catch (e) {
      this.store.rollBack();
      throw new Error(`resetAll failed: ${e}`);
    }
  }

  /** 列出某目录的直接子节点（目录优先，再按名称排序） */
  async listChildren(parentId: number): Promise<NamespaceNode[]> {
    if (this.store === undefined) {
      throw new Error('NamespaceStore not initialized');
    }
    const predicates = new relationalStore.RdbPredicates(TABLE);
    predicates.equalTo('parentId', parentId)
      .orderByDesc('type')   // 'file' > 'dir' 字典序，desc 使 dir 在前
      .orderByAsc('name');
    const rs = await this.store.query(predicates,
      ['id', 'parentId', 'name', 'type', 'size', 'mtime', 'version']);
    const out: NamespaceNode[] = [];
    while (rs.goToNextRow()) {
      out.push(this.rowToNode(rs));
    }
    rs.close();
    return out;
  }

  /** 查找根节点（parentId == 0） */
  async findRoot(): Promise<NamespaceNode | null> {
    if (this.store === undefined) {
      throw new Error('NamespaceStore not initialized');
    }
    const predicates = new relationalStore.RdbPredicates(TABLE);
    predicates.equalTo('parentId', 0);
    const rs = await this.store.query(predicates,
      ['id', 'parentId', 'name', 'type', 'size', 'mtime', 'version']);
    let root: NamespaceNode | null = null;
    if (rs.goToNextRow()) {
      root = this.rowToNode(rs);
    }
    rs.close();
    return root;
  }

  /** 聚合统计（节点数/文件数/文件总大小） */
  async stat(): Promise<NamespaceStat> {
    if (this.store === undefined) {
      throw new Error('NamespaceStore not initialized');
    }
    const rs = await this.store.querySql(`SELECT COUNT(*) AS total,
      SUM(CASE WHEN type='file' THEN 1 ELSE 0 END) AS files,
      SUM(CASE WHEN type='file' THEN size ELSE 0 END) AS bytes FROM ${TABLE}`);
    const s: NamespaceStat = { totalNodes: 0, fileCount: 0, totalSize: 0 };
    if (rs.goToNextRow()) {
      s.totalNodes = rs.getLong(rs.getColumnIndex('total'));
      s.fileCount = rs.getLong(rs.getColumnIndex('files'));
      s.totalSize = rs.getLong(rs.getColumnIndex('bytes'));
    }
    rs.close();
    return s;
  }

  private rowToNode(rs: relationalStore.ResultSet): NamespaceNode {
    return {
      id: rs.getLong(rs.getColumnIndex('id')),
      parentId: rs.getLong(rs.getColumnIndex('parentId')),
      name: rs.getString(rs.getColumnIndex('name')),
      type: rs.getString(rs.getColumnIndex('type')),
      size: rs.getLong(rs.getColumnIndex('size')),
      mtime: rs.getLong(rs.getColumnIndex('mtime')),
      version: rs.getLong(rs.getColumnIndex('version')),
    };
  }
}