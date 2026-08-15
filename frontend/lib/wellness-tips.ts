import type { LucideIcon } from 'lucide-react';
import { AppleIcon, DropletsIcon, Flower2Icon, FootprintsIcon, MoonIcon } from 'lucide-react';

export interface WellnessTipCategory {
  id: string;
  title: string;
  description: string;
  icon: LucideIcon;
  tips: string[];
}

export const WELLNESS_TIP_CATEGORIES: WellnessTipCategory[] = [
  {
    id: 'hydration',
    title: 'Hydration',
    description: 'Drink enough fluids through the day',
    icon: DropletsIcon,
    tips: [
      'Drink a glass of water when you wake up and before each meal.',
      'Keep a water bottle nearby and sip throughout the day.',
      'Water, milk and coconut water all count toward your daily fluids.',
      'Light yellow urine usually means you are well hydrated.',
    ],
  },
  {
    id: 'sleep-rest',
    title: 'Sleep & Rest',
    description: 'Build routines that help you rest well',
    icon: MoonIcon,
    tips: [
      'Try to go to bed and wake up at the same time every day.',
      'Keep screens away for 30 minutes before bedtime.',
      'Make your bedroom dark, quiet and cool for better sleep.',
      'Short 20 to 30 minute naps are better than long daytime naps.',
    ],
  },
  {
    id: 'healthy-eating',
    title: 'Healthy Eating',
    description: 'Simple habits for balanced meals',
    icon: AppleIcon,
    tips: [
      'Fill half your plate with vegetables and fruits at meals.',
      'Prefer whole foods like dal, roti, rice and vegetables over packaged snacks.',
      'Eat at regular times and chew your food slowly.',
      'Limit sugary drinks and fried snacks to special occasions.',
    ],
  },
  {
    id: 'daily-activity',
    title: 'Daily Activity',
    description: 'Move more in everyday life',
    icon: FootprintsIcon,
    tips: [
      'Aim for 30 minutes of walking on most days of the week.',
      'Take short walking breaks if you sit for long hours.',
      'Use stairs instead of the lift when you can.',
      'Do light stretching in the morning and before bed.',
    ],
  },
  {
    id: 'stress-relaxation',
    title: 'Stress & Relaxation',
    description: 'Small ways to unwind and stay calm',
    icon: Flower2Icon,
    tips: [
      'Take slow, deep breaths for a minute when you feel stressed.',
      'Talk to a friend or family member about what is bothering you.',
      'Spend 15 minutes a day on a hobby you enjoy.',
      'Avoid caffeine in the evening so you can relax before sleep.',
    ],
  },
];

export const WELLNESS_DISCLAIMER = 'General wellness information only. Not medical advice.';
