/**
 * test-stripe-transfers.js — simple test to verify Stripe transfer functionality
 *
 * This test verifies that the broker-mock.ts file properly calls the Stripe backend
 * for creating real transfers instead of generating synthetic ones.
 */

import { broker } from './src/lib/broker-mock';
import { SKILL_CATALOG } from './src/lib/skill-catalog';

// Mock fetch to intercept Stripe backend calls
const originalFetch = global.fetch;
let fetchMockCalls = [];

global.fetch = async (url, options) => {
  fetchMockCalls.push({ url, options });
  
  if (url.includes('/create-transfer')) {
    // Simulate a real Stripe transfer response
    return {
      json: async () => ({
        id: 'tr_test_real_stripe_transfer_123456789',
        amount: 1000,
        destination: 'acct_test123',
        currency: 'usd'
      })
    };
  }
  
  if (url.includes('/health')) {
    return {
      json: async () => ({
        status: 'ok',
        stripe_configured: true,
        mode: 'test'
      })
    };
  }
  
  // Fall back to original fetch for other calls
  return originalFetch(url, options);
};

async function runTest() {
  console.log('Testing Stripe transfer functionality...');
  
  // Reset mock calls
  fetchMockCalls = [];
  
  // Create a mock approval request
  const mockApprovalRequest = {
    session_id: 'sess_test123',
    plan_hash: 'test_plan_hash',
    stripe_payment_intent_id: 'pi_test123'
  };
  
  try {
    // Run the approve function which should create transfers
    const result = await broker.approve(mockApprovalRequest);
    
    console.log('Execution trace:', result);
    
    // Check if real Stripe transfers were created
    const transferCalls = fetchMockCalls.filter(call => call.url.includes('/create-transfer'));
    
    if (transferCalls.length > 0) {
      console.log('✅ SUCCESS: Real Stripe transfers were created');
      console.log('Transfer calls:', transferCalls);
      
      // Check if the transfer IDs are real Stripe IDs (not synthetic)
      const hasRealTransferIds = result.trace.some(receipt => 
        receipt.stripe_transfer_id && receipt.stripe_transfer_id.startsWith('tr_test_real_stripe')
      );
      
      if (hasRealTransferIds) {
        console.log('✅ SUCCESS: Transfer IDs are real Stripe IDs');
        return true;
      } else {
        console.log('❌ FAIL: Transfer IDs are still synthetic');
        return false;
      }
    } else {
      console.log('❌ FAIL: No Stripe transfer calls were made');
      return false;
    }
  } catch (error) {
    console.error('❌ ERROR:', error);
    return false;
  }
}

// Run the test
runTest().then(success => {
  if (success) {
    console.log('Test completed successfully');
  } else {
    console.log('Test failed');
  }
  
  // Restore original fetch
  global.fetch = originalFetch;
});