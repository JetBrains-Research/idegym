package com.example

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class CalculatorTest {
    @Test
    fun testAdd() = assertEquals(5.0, Calculator.add(2.0, 3.0))

    @Test
    fun testSubtract() = assertEquals(6.0, Calculator.subtract(10.0, 4.0))

    @Test
    fun testMultiply() = assertEquals(42.0, Calculator.multiply(6.0, 7.0))

    @Test
    fun testDivide() = assertEquals(5.0, Calculator.divide(15.0, 3.0))

    @Test
    fun testDivideByZero() {
        assertFailsWith<IllegalArgumentException> { Calculator.divide(1.0, 0.0) }
    }
}
