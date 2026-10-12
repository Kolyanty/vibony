import { describe, expect, it } from 'vitest';
import { resolveLang } from '../lib/prism';

describe('resolveLang', () => {
  it('resolves common extensions', () => {
    expect(resolveLang('train.py')).toBe('python');
    expect(resolveLang('app.ts')).toBe('typescript');
    expect(resolveLang('App.tsx')).toBe('tsx');
    expect(resolveLang('main.go')).toBe('go');
    expect(resolveLang('lib.rs')).toBe('rust');
    expect(resolveLang('Main.kt')).toBe('kotlin');
    expect(resolveLang('config.yaml')).toBe('yaml');
    expect(resolveLang('config.yml')).toBe('yaml');
    expect(resolveLang('package.json')).toBe('json');
    expect(resolveLang('settings.toml')).toBe('toml');
  });

  it('resolves nested paths via basename', () => {
    expect(resolveLang('src/components/Foo.tsx')).toBe('tsx');
    expect(resolveLang('a/b/c/d.py')).toBe('python');
  });

  it('handles uppercase extensions', () => {
    expect(resolveLang('README.MD')).toBe('markdown');
    expect(resolveLang('Config.JSON')).toBe('json');
  });

  it('handles special filenames without extension', () => {
    expect(resolveLang('Dockerfile')).toBe('docker');
    expect(resolveLang('Makefile')).toBe('makefile');
    expect(resolveLang('infra/Dockerfile')).toBe('docker');
  });

  it('maps terraform extensions to hcl', () => {
    expect(resolveLang('main.tf')).toBe('hcl');
    expect(resolveLang('vars.tfvars')).toBe('hcl');
  });

  it('returns null for unknown extensions', () => {
    expect(resolveLang('weird.xyz')).toBeNull();
    expect(resolveLang('binary.bin')).toBeNull();
  });

  it('returns null for empty / extension-less paths', () => {
    expect(resolveLang('')).toBeNull();
    expect(resolveLang('LICENSE')).toBeNull();
    expect(resolveLang('foo.')).toBeNull();
  });
});
