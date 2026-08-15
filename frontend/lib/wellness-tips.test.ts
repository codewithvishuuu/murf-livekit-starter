import { describe, expect, it } from 'vitest';
import { WELLNESS_DISCLAIMER, WELLNESS_TIP_CATEGORIES } from './wellness-tips';

const EXPECTED_TITLES = [
  'Hydration',
  'Sleep & Rest',
  'Healthy Eating',
  'Daily Activity',
  'Stress & Relaxation',
];

describe('wellness tips content', () => {
  it('covers the five required wellness categories in order', () => {
    expect(WELLNESS_TIP_CATEGORIES.map((category) => category.title)).toEqual(EXPECTED_TITLES);
  });

  it('gives every category a unique id and a non-empty description', () => {
    const ids = WELLNESS_TIP_CATEGORIES.map((category) => category.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const category of WELLNESS_TIP_CATEGORIES) {
      expect(category.description.length).toBeGreaterThan(0);
    }
  });

  it('has a few short, easy-to-read tips per category', () => {
    for (const category of WELLNESS_TIP_CATEGORIES) {
      expect(category.tips.length).toBeGreaterThanOrEqual(3);
      for (const tip of category.tips) {
        expect(tip.length).toBeGreaterThan(0);
        expect(tip.length).toBeLessThanOrEqual(140);
        expect(tip.endsWith('.')).toBe(true);
      }
    }
  });

  it('keeps the disclaimer wording', () => {
    expect(WELLNESS_DISCLAIMER).toBe('General wellness information only. Not medical advice.');
  });
});
